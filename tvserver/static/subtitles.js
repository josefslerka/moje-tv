/* Broadcaster tracks first; lazy ASR from already buffered MP4 audio. */
(function(root){
'use strict';
const BASE_MODES = ['auto','off','asr','translate-cs','translate-cs-api'];
const AI_MODES = ['asr','translate-cs','translate-cs-api'];
class TVSubtitles {
  constructor(video, select, status, retry){
    this.video = video; this.select = select; this.status = status; this.retry = retry;
    this.channel = null; this.hls = null; this.epoch = 0; this.session = null;
    this.fragments = new Map(); this.parts = new Map(); this.init = null;
    this.sent = new Set(); this.lastEnd = -Infinity; this.busy = false;
    this.failed = false; this.backend = null; this.listeners = []; this.bad = new Set();
    this.retryAt = 0; this.retryCount = 0;
    this.oldTracks = new Set(); this.aiTrack = null; this.aiTracks = new Map();
    this.active = ''; this.deadline = Infinity;
    this.mode = 'off';
    select.addEventListener('change', () => {
      this.mode = select.value; this.failed = false;
      this.retryAt = 0; this.retryCount = 0;
      if (AI_MODES.includes(this.mode)) this.deadline = 0;
      this.reconcile();
    });
    retry.addEventListener('click', () => {
      this.failed = false; this.retryAt = 0; this.retryCount = 0;
      this.stopASR(); this.checkBackend().then(() => this.reconcile());
    });
    video.textTracks.addEventListener('addtrack', () => this.reconcile());
    video.textTracks.addEventListener('removetrack', () => this.reconcile());
    video.addEventListener('playing', () => {
      if (this.deadline === Infinity) this.deadline = performance.now() + 4000;
      this.reconcile(); this.pump();
    });
    video.addEventListener('pause', () => {
      if (this.channel !== null && this.isPaid){
        this.stopASR(); this.say('Pozastaveno · další text se do API neposílá.');
      }
    });
    video.addEventListener('timeupdate', () => { this.pruneCues(); this.pump(); });
    video.addEventListener('seeking', () => {
      this.stopASR(); this.sent.clear(); this.lastEnd = -Infinity;
    });
    video.addEventListener('seeked', () => this.reconcile());
    root.addEventListener('pagehide', () => { this.stopASR(); clearInterval(this.timer); });
    this.timer = setInterval(() => this.reconcile(), 1000);
    this.render(); this.checkBackend();
  }
  get asrLanguage(){
    return this.backend?.auto_language ? 'auto' : 'en';
  }
  get isPaid(){ return this.mode === 'translate-cs-api'; }
  get translationTarget(){ return ['translate-cs','translate-cs-api'].includes(this.mode) ? 'cs' : null; }
  get translationProvider(){ return this.isPaid ? 'openrouter' : 'local'; }
  get asrLabel(){
    if (this.isPaid) return 'Překlad do češtiny · Gemini (placené API)';
    return this.translationTarget ? 'Překlad do češtiny' : 'Titulky';
  }
  get isASR(){ return AI_MODES.includes(this.active); }
  say(text, error=false){
    if (this.isPaid && text && !text.includes('placené API')) text = 'Gemini · placené API · '+text;
    this.status.textContent = text;
    this.status.classList.toggle('err', error);
    this.retry.hidden = !error || !this.channel;
  }
  async checkBackend(){
    try { const r = await fetch('/api/captions/status', {cache:'no-store'}); this.backend = r.ok ? await r.json() : {available:false}; }
    catch(e){ this.backend = {available:false}; }
    this.reconcile();
  }
  beginChannel(){
    this.epoch++; this.stopASR();
    this.channel = null; this.active = ''; this.deadline = Infinity;
    this.fragments.clear(); this.parts.clear(); this.init = null; this.bad.clear(); this.failed = false;
    this.retryAt = 0; this.retryCount = 0;
    this.sent.clear(); this.lastEnd = -Infinity;
    if (!BASE_MODES.includes(this.mode)) this.mode = 'auto';
    for (const [event, handler] of this.listeners) this.hls?.off(event, handler);
    this.listeners = []; this.hls = null;
    this.oldTracks = new Set(Array.from(this.video.textTracks));
    for (const track of this.video.textTracks){
      track.mode = 'disabled';
      try { for (const cue of Array.from(track.cues || [])) track.removeCue(cue); } catch(e){}
    }
    this.render(); this.say('');
  }
  attach(channel, hls){
    this.beginChannel(); this.channel = channel; this.hls = hls;
    if (hls){
      const epoch = this.epoch;
      const on = (event, fn) => {
        if (!event) return;
        const handler = (_, data) => { if (epoch === this.epoch) fn(data); };
        hls.on(event, handler); this.listeners.push([event, handler]);
      };
      on(Hls.Events.SUBTITLE_TRACKS_UPDATED, () => this.reconcile());
      on(Hls.Events.SUBTITLE_TRACK_LOADED, d => { this.bad.delete(d.id); this.reconcile(); });
      on(Hls.Events.SUBTITLE_FRAG_PROCESSED, d => {
        if (!d.success) this.bad.add(hls.subtitleTrack);
        this.reconcile();
      });
      on(Hls.Events.ERROR, d => {
        if (/subtitle/i.test(d.details || '')){
          this.bad.add(hls.subtitleTrack); this.reconcile();
        }
      });
      on(Hls.Events.BUFFER_CODECS, d => {
        const track = d.audio || d.audiovideo;
        if (track?.initSegment?.byteLength) this.init = new Uint8Array(track.initSegment);
      });
      on(Hls.Events.BUFFER_APPENDING, d => this.audioPart(d));
      on(Hls.Events.FRAG_BUFFERED, d => this.audioReady(d.frag));
      on(Hls.Events.AUDIO_TRACK_SWITCHING, () => {
        this.stopASR(); this.fragments.clear(); this.parts.clear(); this.sent.clear(); this.lastEnd = -Infinity;
      });
    }
    this.reconcile();
  }
  tracks(){
    const tracks = (this.hls?.subtitleTracks || []).map((track, index) => ({
      key:'hls:'+index, label:track.name || track.lang || ('Titulky '+(index+1)),
      lang:track.lang || '', index, disabled:this.bad.has(index),
    }));
    Array.from(this.video.textTracks).forEach((track, index) => {
      if (track === this.aiTrack || Array.from(this.aiTracks.values()).includes(track)
          || !['subtitles','captions'].includes(track.kind)) return;
      if (this.oldTracks.has(track) && !(track.cues?.length)) return;
      if (tracks.some(t => t.label === track.label && t.lang === track.language)) return;
      tracks.push({key:'native:'+index, label:track.label || track.language || ('Titulky '+(index+1)),
                   lang:track.language || '', track, disabled:false});
    });
    return tracks;
  }
  render(tracks=this.tracks()){
    const signature = JSON.stringify([this.channel, this.mode, !!this.hls, this.backend?.available, this.backend?.languages,
                                     this.backend?.translation, this.backend?.cloud_translation, this.backend?.auto_language,
                                     tracks.map(t => [t.key,t.label,t.disabled])]);
    if (signature === this.signature) return;
    this.signature = signature;
    const option = (value, text) => new Option(text, value);
    const entries = [option('auto','Automaticky'), option('off','Vypnuto')];
    if (tracks.length){
      const group = document.createElement('optgroup'); group.label = 'Od vysílatele';
      tracks.forEach(track => { const o = option(track.key,track.label); o.disabled = track.disabled; group.append(o); });
      entries.push(group);
    }
    const ai = option('asr','Titulky');
    ai.title = 'Rozpozná jazyk řeči a vytvoří titulky v původním jazyce.';
    ai.disabled = !this.hls || this.backend?.available === false;
    const translation = option('translate-cs','Zdarma · lokálně → čeština');
    translation.title = 'Lokální překlad bez poplatků za API. Překlad může měnit význam.';
    translation.disabled = ai.disabled || this.backend?.translation !== true;
    const cloud = option('translate-cs-api','Placené API · Gemini → čeština');
    cloud.title = 'Gemini přes OpenRouter: platí se za odeslaný text. Zvuk se přepisuje lokálně. Překlad může měnit význam.';
    cloud.disabled = ai.disabled || this.backend?.cloud_translation !== true;
    entries.push(ai,translation,cloud); this.select.replaceChildren(...entries);
    this.retry.textContent = this.translationTarget ? 'Zkusit překlad znovu' : 'Zkusit titulky znovu';
    this.select.disabled = !this.channel;
    if (!Array.from(this.select.options).some(o => o.value === this.mode)) this.mode = 'auto';
    this.select.value = this.mode;
  }
  reconcile(){
    if (!this.channel) return;
    const tracks = this.tracks(); this.render(tracks);
    if (this.mode === 'off'){
      this.disableTracks(); this.stopASR(); this.active = 'off'; this.say('Titulky jsou vypnuté.'); return;
    }
    let chosen = tracks.find(t => t.key === this.mode && !t.disabled);
    if (this.mode === 'auto'){
      const language = (navigator.language || 'en').split('-')[0];
      chosen = tracks.find(t => !t.disabled && t.lang.split('-')[0] === language)
            || tracks.find(t => !t.disabled && t.lang.startsWith('en')) || tracks.find(t => !t.disabled);
    }
    if (chosen){
      this.stopASR();
      if (this.active !== chosen.key){
        this.disableTracks();
        if (chosen.key.startsWith('hls:')){
          this.hls.subtitleDisplay = true; this.hls.subtitleTrack = chosen.index;
        } else chosen.track.mode = 'showing';
        this.active = chosen.key;
      }
      this.say('Od vysílatele · '+chosen.label); return;
    }
    if (!AI_MODES.includes(this.mode) && (performance.now() < this.deadline || this.video.paused)){
      this.say('Hledám titulky vysílatele…'); return;
    }
    const aiKey = AI_MODES.includes(this.mode) ? this.mode : 'asr';
    if (this.active !== aiKey){
      this.stopASR(); this.disableTracks(); this.active = aiKey;
    }
    if (!this.hls){ this.say('AI přepis není v tomto prohlížeči dostupný.',true); return; }
    if (!this.backend){ this.say('Ověřuji dostupnost přepisu…'); return; }
    if (!this.backend.available){ this.say('AI přepis není na serveru dostupný.',true); return; }
    if (this.translationTarget && (this.isPaid ? this.backend.cloud_translation : this.backend.translation) !== true){
      this.say(this.isPaid ? 'Placený překlad není na serveru dostupný.' : 'Český překlad není na serveru dostupný.',true); return;
    }
    if (this.failed && this.retryAt && performance.now() >= this.retryAt){
      this.failed = false; this.retryAt = 0;
    }
    if (!this.failed) this.ensureASR();
  }
  disableTracks(){
    if (this.hls){ this.hls.subtitleDisplay = false; this.hls.subtitleTrack = -1; }
    for (const track of this.video.textTracks) track.mode = 'disabled';
  }
  stopASR(){
    this.request?.abort(); this.request = null;
    this.opening?.request?.abort();
    const sid = this.session; this.session = null;
    this.opening = null; this.busy = false;
    if (sid) fetch('/api/captions/sessions/'+encodeURIComponent(sid), {method:'DELETE',keepalive:true}).catch(() => {});
    for (const track of new Set([...this.aiTracks.values(),this.aiTrack].filter(Boolean))){
      track.mode = 'disabled';
      for (const cue of Array.from(track.cues || [])) track.removeCue(cue);
    }
  }
  recoverASR(message){
    this.stopASR(); this.failed = true;
    this.retryCount = Math.min(this.retryCount+1,6);
    this.retryAt = performance.now() + Math.min(30000,1000*2**(this.retryCount-1));
    this.say(message+' Automaticky obnovuji…',true);
  }
  async ensureASR(){
    if (this.session){ this.aiTrack.mode = 'showing'; this.pump(); return; }
    if (this.opening || this.video.paused) return;
    const epoch = this.epoch, language = this.asrLanguage, translation = this.translationTarget,
          provider = this.translationProvider, active = this.active;
    const opening = {request:new AbortController()}; this.opening = opening;
    const timeout = setTimeout(() => opening.request.abort(),15000);
    this.say(this.asrLabel+' · načítám…');
    try {
      const response = await fetch('/api/captions/sessions', {method:'POST',headers:{'Content-Type':'application/json'},
                                   body:JSON.stringify({channel_id:this.channel,language,translation,translation_provider:provider}),
                                   signal:opening.request.signal});
      if (!response.ok) throw new Error('Přepis není dostupný.');
      const data = await response.json();
      if (epoch !== this.epoch || this.active !== active || this.opening !== opening
          || this.asrLanguage !== language || this.translationTarget !== translation){
        fetch('/api/captions/sessions/'+encodeURIComponent(data.session),{method:'DELETE'}).catch(() => {}); return;
      }
      if ((data.language && data.language !== language) || (data.translation || null) !== translation
          || (translation && (data.translation_provider || 'local') !== provider)){
        fetch('/api/captions/sessions/'+encodeURIComponent(data.session),{method:'DELETE'}).catch(() => {});
        throw new Error('Server nepodporuje zvolený jazyk přepisu.');
      }
      this.session = data.session; this.sent.clear(); this.lastEnd = -Infinity;
      if (!this.aiTracks.has(active)){
        this.aiTracks.set(active,this.video.addTextTrack('captions',this.asrLabel,translation || (language === 'auto' ? 'und' : language)));
      }
      this.aiTrack = this.aiTracks.get(active);
      this.aiTrack.mode = 'showing';
      this.say(language === 'auto' ? 'Rozpoznávám jazyk řeči…' : this.asrLabel+' · čekám na řeč…');
      this.pump();
    } catch(e){
      if (epoch === this.epoch && this.active === active && this.opening === opening
          && this.asrLanguage === language && this.translationTarget === translation){
        this.recoverASR('AI přepis je dočasně nedostupný.');
      }
    } finally { clearTimeout(timeout); if (this.opening === opening) this.opening = null; }
  }
  audioPart(data){
    if (!this.init || !['audio','audiovideo'].includes(data.type) || !data.frag || !data.data?.byteLength) return;
    const bytes = data.data;
    if (String.fromCharCode(...bytes.subarray(4,8)) === 'ftyp') return;
    const key = this.fragmentKey(data.frag);
    let item = this.parts.get(key);
    if (!item){ item = {init:this.init,parts:[],bytes:0}; this.parts.set(key,item); }
    if ((item.bytes += bytes.byteLength) > 8*1024*1024){ this.parts.delete(key); return; }
    item.parts.push(new Uint8Array(bytes));
    while (this.parts.size > 16) this.parts.delete(this.parts.keys().next().value);
  }
  fragmentKey(frag){ return [frag.type,frag.level,frag.cc,frag.sn].join(':'); }
  audioReady(frag){
    const key = this.fragmentKey(frag), item = this.parts.get(key);
    if (!item) return;
    this.parts.delete(key);
    const audio = frag.elementaryStreams?.audio;
    const start = Number.isFinite(audio?.startPTS) ? audio.startPTS : frag.start;
    const end = Number.isFinite(audio?.endPTS) ? audio.endPTS : start + frag.duration;
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || end-start > 30) return;
    this.fragments.set(key,{key,start,end,blob:new Blob([item.init,...item.parts],{type:'audio/mp4'})});
    const now = this.video.currentTime;
    for (const [old, value] of this.fragments){
      if (value.end < now-40){
        this.fragments.delete(old); this.sent.delete(old);
      }
    }
    let total = 0, kept = 0;
    const distance = f => Math.max(f.start-now,now-f.end,0);
    const played = f => this.sent.has(f.key) && f.end <= now;
    for (const f of Array.from(this.fragments.values()).sort((a,b) =>
      Number(played(a))-Number(played(b)) || distance(a)-distance(b) || a.start-b.start)){
      if (kept < 16 && total+f.blob.size <= 16*1024*1024){
        total += f.blob.size; kept++;
      } else {
        this.fragments.delete(f.key); this.sent.delete(f.key);
      }
    }
    this.pump();
  }
  async pump(){
    if (!this.session || this.busy || this.video.paused || this.video.seeking || !this.isASR || this.failed) return;
    const now = this.video.currentTime;
    const item = Array.from(this.fragments.values()).filter(f => !this.sent.has(f.key) && f.end > now-3 &&
      f.end > this.lastEnd+.1 && f.start < now+16).sort((a,b) => a.start-b.start)[0];
    if (!item) return;
    this.busy = true; this.sent.add(item.key);
    const epoch = this.epoch, sid = this.session, request = new AbortController(); this.request = request;
    let timedOut = false, timeout;
    const progress = () => {
      clearTimeout(timeout);
      timeout = setTimeout(() => { timedOut = true; request.abort(); },45000);
    };
    progress();
    try {
      const response = await fetch('/api/captions/sessions/'+encodeURIComponent(sid)+'/audio?start='+item.start+'&end='+item.end,
                                   {method:'POST',body:item.blob,signal:request.signal});
      if (response.status === 404 && epoch === this.epoch && sid === this.session){
        this.stopASR(); this.ensureASR(); return;
      }
      if (!response.ok || !response.body) throw new Error('Přepis není dostupný.');
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
      while (true){
        const {value,done} = await reader.read(); if (done) break;
        progress();
        buffer += decoder.decode(value,{stream:true});
        let newline;
        while ((newline=buffer.indexOf('\n')) >= 0){
          const line = buffer.slice(0,newline); buffer=buffer.slice(newline+1);
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          if (epoch !== this.epoch || sid !== this.session) return;
          if (event.type === 'error') throw new Error(event.message);
          if (event.type === 'cue'){
            this.retryCount = 0; this.retryAt = 0; this.cue(event);
          }
        }
      }
      this.lastEnd = item.end;
    } catch(e){
      if ((timedOut || e.name !== 'AbortError') && epoch === this.epoch && sid === this.session){
        this.recoverASR('AI přepis se přerušil.');
      }
    } finally {
      clearTimeout(timeout);
      if (this.request === request){ this.request = null; this.busy = false; this.pump(); }
    }
  }
  cue(event){
    if (!event.text || !this.aiTrack || !this.isASR) return;
    const now = this.video.currentTime;
    if (event.end < now-6) return;
    const start = Math.max(event.start,now), end = Math.max(event.end,start+1.5);
    for (const previous of Array.from(this.aiTrack.cues || [])){
      if (previous.startTime < start && previous.endTime > start) previous.endTime = start;
    }
    const source = event.source_language || event.detected_language ||
      (event.language !== this.translationTarget ? event.language : null);
    const fallback = this.translationTarget && (event.translation_failed || event.translation_unsupported);
    const content = (fallback ? '['+(source || 'en').toUpperCase()+'] ' : '') + event.text;
    const text = content.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const cue = new VTTCue(start,end,text); cue.line = -3;
    this.aiTrack.addCue(cue); this.aiTrack.mode = 'showing';
    let label = this.asrLabel;
    if (this.translationTarget){
      if (event.translation_unsupported){
        label = this.languageLabel(source)+' · přepis v originále; překlad zatím nepodporujeme.';
        if (this.isPaid) label += ' API se nevolá.';
      } else if (event.translation_failed){
        label = 'Překlad do češtiny se obnovuje · zatím '+(source === 'en' ? 'anglický přepis.' : 'přepis v originále.');
      } else if (event.translation_skipped){
        label = this.isPaid ? 'Čeština · přepis bez volání API.' : 'Čeština · přepis bez překladu.';
      } else if (source){
        label = this.languageLabel(source)+' → čeština · experimentální překlad';
      }
    } else if (source){
      label = this.languageLabel(source)+' · titulky';
    }
    this.say(label,!!event.translation_failed);
    this.pruneCues();
  }
  languageLabel(code){
    try {
      const name = new Intl.DisplayNames(['cs'],{type:'language'}).of(code);
      return name.charAt(0).toUpperCase()+name.slice(1);
    } catch(e){ return (code || 'Neznámý jazyk').toUpperCase(); }
  }
  pruneCues(){
    if (!this.aiTrack) return;
    for (const cue of Array.from(this.aiTrack.cues || [])){
      if (cue.endTime < this.video.currentTime-20 || this.aiTrack.cues.length > 150) this.aiTrack.removeCue(cue);
    }
  }
}
root.TVSubtitles = TVSubtitles;
if (typeof module !== 'undefined') module.exports = {TVSubtitles};
})(typeof window !== 'undefined' ? window : globalThis);
