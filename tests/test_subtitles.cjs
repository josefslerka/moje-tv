const test = require('node:test');
const assert = require('node:assert/strict');
const {TVSubtitles} = require('../tvserver/static/subtitles.js');

class Target extends EventTarget {
  constructor(){ super(); this.hidden=false; this.textContent=''; this.classList={toggle(){}}; }
}
class Option {
  constructor(text,value){ this.text=text; this.value=value; this.disabled=false; }
}
class Select extends Target {
  constructor(){ super(); this.children=[]; this.value='auto'; }
  replaceChildren(...children){ this.children=children; }
  get options(){ return this.children.flatMap(c => c.children || [c]); }
}
class Track {
  constructor(label='English',kind='captions',language='en'){
    Object.assign(this,{label,kind,language,mode:'disabled',cues:[]});
  }
  addCue(cue){ this.cues.push(cue); }
  removeCue(cue){ this.cues.splice(this.cues.indexOf(cue),1); }
}
class Video extends Target {
  constructor(){
    super(); this.currentTime=10; this.paused=false; this.seeking=false;
    this.textTracks=[]; const events=new EventTarget();
    this.textTracks.addEventListener=events.addEventListener.bind(events);
    this.textTracks.emit=type => events.dispatchEvent(new Event(type));
  }
  addTextTrack(kind,label,language){
    const track=new Track(label,kind,language); this.textTracks.push(track);
    queueMicrotask(() => this.textTracks.emit('addtrack')); return track;
  }
}
class Hls {
  static Events=Object.fromEntries(['SUBTITLE_TRACKS_UPDATED','SUBTITLE_TRACK_LOADED','SUBTITLE_FRAG_PROCESSED',
    'ERROR','BUFFER_CODECS','BUFFER_APPENDING','FRAG_BUFFERED','AUDIO_TRACK_SWITCHING'].map(k => [k,k]));
  constructor(){ this.subtitleTracks=[]; this.handlers=new Map(); this.subtitleTrack=-1; }
  on(name,fn){ this.handlers.set(name,fn); }
  off(name){ this.handlers.delete(name); }
  emit(name,data){ this.handlers.get(name)?.(name,data); }
}
const tick=() => new Promise(resolve => setImmediate(resolve));

function fixture(customFetch, {mode='auto',storedMode}={}){
  const requests=[], storage=new Map();
  if (storedMode) storage.set('subtitleMode',storedMode);
  globalThis.Option=Option; globalThis.Hls=Hls;
  globalThis.document={createElement(){ return {children:[],append(child){ this.children.push(child); }}; }};
  globalThis.localStorage={getItem:k => storage.get(k),setItem:(k,v) => storage.set(k,v)};
  Object.defineProperty(globalThis,'navigator',{value:{language:'en-US'},configurable:true});
  globalThis.addEventListener=() => {};
  globalThis.setInterval=() => 0;
  globalThis.VTTCue=class { constructor(startTime,endTime,text){ Object.assign(this,{startTime,endTime,text}); } };
  globalThis.fetch=async (url,options={}) => {
    requests.push({url,options});
    const custom=customFetch?.(url,options); if (custom) return custom;
    const body=options.body && typeof options.body==='string' ? JSON.parse(options.body) : {};
    const language=body.language || 'en', translation=body.translation || false, provider=body.translation_provider || 'local';
    return {ok:true,status:200,json:async () => url.endsWith('/status') ? {available:true,languages:['en','cs'],translation:true,cloud_translation:true,auto_language:true}
            : {session:provider==='openrouter' ? 'paid-session' : translation ? 'translation-session' : language==='cs' ? 'czech-session' : 'test-session',
               language,translation,translation_provider:translation ? provider : null,paid:provider==='openrouter'}};
  };
  const video=new Video(), select=new Select(), status=new Target(), retry=new Target();
  const controller=new TVSubtitles(video,select,status,retry);
  if (mode !== null){ select.value=mode; select.dispatchEvent(new Event('change')); }
  return {controller,video,select,status,retry,requests,hls:new Hls()};
}

test('automatic mode chooses broadcaster tracks without loading ASR',async () => {
  const f=fixture(); f.hls.subtitleTracks=[{name:'English',lang:'en'}];
  f.controller.attach(17,f.hls); await tick();
  assert.equal(f.hls.subtitleTrack,0);
  assert.equal(f.hls.subtitleDisplay,true);
  assert.equal(f.controller.active,'hls:0');
  assert.equal(f.requests.filter(r => r.options.method==='POST').length,0);
});

test('late embedded captions replace ASR and delete its session',async () => {
  const f=fixture(); f.controller.attach(17,f.hls); await tick();
  f.controller.deadline=0; f.controller.reconcile(); await tick();
  assert.equal(f.controller.session,'test-session');
  assert.equal(f.controller.tracks().length,0); // Its own AI track is not a broadcaster track.
  const cc=f.video.addTextTrack('captions','CC1','en'); await tick();
  assert.equal(f.controller.session,null);
  assert.equal(cc.mode,'showing');
  assert.equal(f.controller.aiTrack.mode,'disabled');
  assert.ok(f.requests.some(r => r.options.method==='DELETE'));
});

test('a broken broadcaster track falls back to ASR and takes priority again after recovery',async () => {
  const f=fixture(); f.hls.subtitleTracks=[{name:'English',lang:'en'}];
  f.controller.attach(17,f.hls); await tick();
  f.controller.deadline=0;
  f.hls.emit(Hls.Events.ERROR,{details:'subtitleTrackLoadError'}); await tick();
  assert.equal(f.controller.active,'asr');
  assert.equal(f.controller.session,'test-session');
  f.hls.emit(Hls.Events.SUBTITLE_TRACK_LOADED,{id:0}); await tick();
  assert.equal(f.controller.active,'hls:0');
  assert.equal(f.controller.session,null);
});

test('off stops ASR and stays off after changing channels',async () => {
  const f=fixture(undefined,{mode:null,storedMode:'asr-cs'});
  f.controller.attach(17,f.hls); f.controller.deadline=0; await tick();
  assert.equal(f.controller.mode,'off');
  assert.equal(f.select.value,'off');
  assert.equal(f.requests.filter(r => r.options.method==='POST').length,0);
  f.controller.mode='asr'; f.controller.reconcile(); await tick();
  f.select.value='off'; f.select.dispatchEvent(new Event('change')); await tick();
  f.controller.attach(18,new Hls()); f.controller.deadline=0; f.controller.reconcile(); await tick();
  assert.equal(f.controller.mode,'off');
  assert.equal(f.controller.session,null);
  assert.equal(f.controller.active,'off');
});

test('original subtitles detect the spoken language without requesting translation',async () => {
  const f=fixture(); f.controller.attach(17,f.hls); await tick();
  f.controller.mode='asr'; f.controller.reconcile(); await tick();
  const original=f.controller.aiTrack;
  assert.equal(original.language,'und');
  assert.equal(original.label,'Titulky');
  assert.ok(f.requests.some(r => r.options.method==='POST' &&
    JSON.parse(r.options.body).language==='auto' && !JSON.parse(r.options.body).translation));
  f.controller.cue({start:10,end:12,text:'Bonjour',detected_language:'fr',language:'fr'});
  assert.equal(original.cues[0].text,'Bonjour');
  assert.equal(f.status.textContent,'Francouzština · titulky');
  assert.equal(f.select.options.some(o => o.value==='asr-cs'),false);
  f.controller.attach(18,new Hls()); await tick();
  assert.equal(f.controller.mode,'asr');
  assert.equal(f.controller.aiTrack,original);
  assert.equal(original.cues.length,0);
  assert.equal(f.controller.tracks().length,0);
});

test('an old session response cannot attach itself to a new channel',async () => {
  let finish;
  const f=fixture((url,options) => {
    if (url==='/api/captions/sessions' && options.method==='POST') return new Promise(resolve => {finish=resolve;});
  });
  f.controller.attach(17,f.hls); await tick();
  f.controller.deadline=0; f.controller.reconcile(); await tick();
  f.controller.beginChannel();
  finish({ok:true,json:async () => ({session:'stale-session'})}); await tick();
  assert.equal(f.controller.session,null);
  assert.ok(f.requests.some(r => r.url.endsWith('/stale-session') && r.options.method==='DELETE'));
});

test('audio fragments retain their MP4 init and actual media timestamps',async () => {
  const f=fixture(); f.video.paused=true; f.controller.attach(17,f.hls); await tick();
  const init=Uint8Array.from([0,0,0,8,102,116,121,112]);
  const moof=Uint8Array.from([0,0,0,8,109,111,111,102]);
  const mdat=Uint8Array.from([0,0,0,8,109,100,97,116]);
  const frag={type:'main',level:0,cc:0,sn:4,start:20,duration:6,
              elementaryStreams:{audio:{startPTS:23.1,endPTS:29.1}}};
  f.hls.emit(Hls.Events.BUFFER_CODECS,{audio:{initSegment:init}});
  for (const data of [init,moof,mdat]) f.hls.emit(Hls.Events.BUFFER_APPENDING,{type:'audio',frag,data});
  f.hls.emit(Hls.Events.FRAG_BUFFERED,{frag});
  const item=Array.from(f.controller.fragments.values())[0];
  assert.equal(item.start,23.1); assert.equal(item.end,29.1);
  assert.equal(item.blob.size,24); // The init is included once, not twice.
  assert.deepEqual(new Uint8Array(await item.blob.arrayBuffer()),Uint8Array.from([...init,...moof,...mdat]));
});

test('switching channels removes all old AI cues',async () => {
  const f=fixture(); f.controller.attach(17,f.hls); await tick();
  f.controller.aiTrack=f.video.addTextTrack('captions','Anglický přepis (AI)','en');
  f.controller.active='asr'; f.controller.cue({start:10,end:12,text:'Old channel'});
  assert.equal(f.controller.aiTrack.cues.length,1);
  f.controller.attach(18,new Hls()); await tick();
  assert.equal(f.controller.aiTrack.cues.length,0);
  assert.equal(f.controller.aiTrack.mode,'disabled');
});

test('late cues are visible immediately and cannot overlap their successor',async () => {
  const f=fixture(); f.controller.attach(17,f.hls); await tick();
  f.controller.aiTrack=f.video.addTextTrack('captions','Anglický přepis (AI)','en');
  f.controller.active='asr'; f.controller.cue({start:6,end:8,text:'<first>'});
  const first=f.controller.aiTrack.cues[0];
  assert.equal(first.startTime,10); assert.equal(first.endTime,11.5);
  assert.equal(first.text,'&lt;first&gt;');
  f.video.currentTime=11; f.controller.cue({start:8,end:10,text:'Second'});
  assert.equal(first.endTime,11);
});

test('translation stays off on reload even when the model is available',async () => {
  const f=fixture(undefined,{mode:null,storedMode:'translate-cs'});
  f.controller.attach(17,f.hls); f.controller.deadline=0; await tick();
  assert.equal(f.controller.mode,'off');
  assert.equal(f.select.options.find(o => o.value==='translate-cs').disabled,false);
  assert.equal(f.requests.filter(r => r.options.method==='POST').length,0);
});

test('paid translation is clearly labelled and never restored on page load',async () => {
  const f=fixture(undefined,{mode:null,storedMode:'translate-cs-api'});
  f.controller.attach(17,f.hls); f.controller.deadline=0; await tick();
  assert.equal(f.controller.mode,'off');
  assert.match(f.select.options.find(o => o.value==='translate-cs').text,/Zdarma.*lokálně/);
  assert.match(f.select.options.find(o => o.value==='translate-cs-api').text,/Placené API.*Gemini/);
  assert.equal(f.requests.filter(r => r.options.method==='POST').length,0);
});

test('only the separately selected paid option requests the OpenRouter provider',async () => {
  const f=fixture(undefined,{mode:'translate-cs-api'}); f.controller.attach(17,f.hls); await tick();
  const created=f.requests.find(r => r.options.method==='POST');
  assert.equal(JSON.parse(created.options.body).translation_provider,'openrouter');
  assert.equal(JSON.parse(created.options.body).translation,'cs');
  assert.equal(f.controller.session,'paid-session');
  assert.match(f.status.textContent,/Gemini.*placené API/);
  f.controller.cue({start:10,end:12,text:'Placený překlad',source_language:'fr',translation:'cs'});
  assert.match(f.status.textContent,/Gemini.*placené API/);
  f.select.value='translate-cs'; f.select.dispatchEvent(new Event('change')); await tick();
  assert.equal(f.controller.session,'translation-session');
  assert.ok(f.requests.some(r => r.url.endsWith('/paid-session') && r.options.method==='DELETE'));
  assert.equal(JSON.parse(f.requests.filter(r => r.options.method==='POST').at(-1).options.body).translation_provider,'local');
  assert.doesNotMatch(f.status.textContent,/placené API/);
});

test('a pending paid session cannot replace a newly selected free session',async () => {
  let finish;
  const f=fixture((url,options) => {
    if (url==='/api/captions/sessions' && options.method==='POST'
        && JSON.parse(options.body).translation_provider==='openrouter') return new Promise(resolve => {finish=resolve;});
  },{mode:'translate-cs-api'});
  f.controller.attach(17,f.hls); await tick();
  f.select.value='translate-cs'; f.select.dispatchEvent(new Event('change')); await tick();
  finish({ok:true,json:async () => ({session:'late-paid',language:'auto',translation:'cs',translation_provider:'openrouter',paid:true})});
  await tick();
  assert.equal(f.controller.session,'translation-session');
  assert.ok(f.requests.some(r => r.url.endsWith('/late-paid') && r.options.method==='DELETE'));
});

test('a failed local translator never falls back to the paid provider',async () => {
  const f=fixture(url => url.endsWith('/status') ? {ok:true,json:async () => ({
    available:true,translation:false,cloud_translation:true,auto_language:true})} : null,{mode:'translate-cs'});
  f.controller.attach(17,f.hls); await tick();
  assert.equal(f.select.options.find(o => o.value==='translate-cs-api').disabled,false);
  assert.equal(f.requests.filter(r => r.options.method==='POST').length,0);
  assert.equal(f.controller.mode,'translate-cs');
});

test('paid translation works independently of local translation availability',async () => {
  const f=fixture(url => url.endsWith('/status') ? {ok:true,json:async () => ({
    available:true,translation:false,cloud_translation:true,auto_language:true})} : null,{mode:'translate-cs-api'});
  f.controller.attach(17,f.hls); await tick();
  assert.equal(f.controller.session,'paid-session');
  assert.equal(f.select.options.find(o => o.value==='translate-cs').disabled,true);
});

test('missing paid configuration does not silently start a different translation mode',async () => {
  const f=fixture(url => url.endsWith('/status') ? {ok:true,json:async () => ({
    available:true,translation:true,cloud_translation:false,auto_language:true})} : null,{mode:'translate-cs-api'});
  f.controller.attach(17,f.hls); await tick();
  assert.equal(f.requests.filter(r => r.options.method==='POST').length,0);
  assert.match(f.status.textContent,/placené API/);
});

test('paid selection rejects a session that does not explicitly confirm its provider',async () => {
  const f=fixture((url,options) => url==='/api/captions/sessions' && options.method==='POST'
    ? {ok:true,json:async () => ({session:'wrong-provider',language:'auto',translation:'cs'})} : null,{mode:'translate-cs-api'});
  f.controller.attach(17,f.hls); await tick();
  assert.equal(f.controller.session,null);
  assert.ok(f.requests.some(r => r.url.endsWith('/wrong-provider') && r.options.method==='DELETE'));
});

test('turning paid subtitles off aborts audio and cannot submit further paid fragments',async () => {
  let aborted=false;
  const f=fixture((url,options) => url.includes('/audio?') ? new Promise((resolve,reject) => {
    options.signal.addEventListener('abort',() => {aborted=true; reject(Object.assign(new Error(),{name:'AbortError'}));});
  }) : null,{mode:'translate-cs-api'});
  f.controller.attach(17,f.hls); await tick();
  f.controller.fragments.set('first',{key:'first',start:10,end:12,blob:new Blob(['audio'])});
  const pending=f.controller.pump(); await tick();
  f.select.value='off'; f.select.dispatchEvent(new Event('change')); await pending; await tick();
  f.controller.fragments.set('second',{key:'second',start:12,end:14,blob:new Blob(['audio'])});
  await f.controller.pump();
  assert.ok(aborted);
  assert.equal(f.requests.filter(r => r.url.includes('/audio?')).length,1);
  assert.equal(f.controller.session,null);
  assert.doesNotMatch(f.status.textContent,/placené API/);
});

test('pausing paid subtitles closes the session and playing resumes only the paid selection',async () => {
  const f=fixture(undefined,{mode:'translate-cs-api'}); f.controller.attach(17,f.hls); await tick();
  f.video.paused=true; f.video.dispatchEvent(new Event('pause')); await tick();
  assert.equal(f.controller.session,null);
  assert.match(f.status.textContent,/Pozastaveno/);
  assert.ok(f.requests.some(r => r.url.endsWith('/paid-session') && r.options.method==='DELETE'));
  f.controller.reconcile(); await tick();
  assert.equal(f.requests.filter(r => r.options.method==='POST').length,1);
  f.video.paused=false; f.video.dispatchEvent(new Event('playing')); await tick();
  assert.equal(f.controller.session,'paid-session');
  assert.equal(f.requests.filter(r => r.options.method==='POST').length,2);
});

test('translation uses a separate Czech track and switching back reuses original subtitles',async () => {
  const f=fixture(); f.controller.attach(17,f.hls); await tick();
  f.select.value='asr'; f.select.dispatchEvent(new Event('change')); await tick();
  const original=f.controller.aiTrack;
  f.controller.cue({start:10,end:12,text:'Original'});
  f.select.value='translate-cs'; f.select.dispatchEvent(new Event('change')); await tick();
  assert.equal(f.controller.session,'translation-session');
  assert.equal(f.controller.active,'translate-cs');
  assert.equal(f.controller.aiTrack.language,'cs');
  assert.equal(f.controller.aiTrack.label,'Překlad do češtiny');
  assert.notEqual(f.controller.aiTrack,original);
  assert.equal(original.cues.length,0);
  assert.equal(original.mode,'disabled');
  assert.ok(f.requests.some(r => r.options.method==='POST' &&
    JSON.parse(r.options.body).translation==='cs' && JSON.parse(r.options.body).language==='auto'));
  f.controller.attach(18,new Hls()); await tick();
  assert.equal(f.controller.mode,'translate-cs');
  f.select.value='asr'; f.select.dispatchEvent(new Event('change')); await tick();
  assert.equal(f.controller.aiTrack,original);
  assert.equal(f.controller.aiTrack.language,'und');
  assert.equal(f.controller.tracks().length,0);
});

test('a pending original-language session cannot replace translation when source modes match',async () => {
  let finish;
  const f=fixture((url,options) => {
    if (url==='/api/captions/sessions' && options.method==='POST' && !JSON.parse(options.body).translation){
      return new Promise(resolve => {finish=resolve;});
    }
  });
  f.controller.attach(17,f.hls); await tick();
  f.controller.deadline=0; f.controller.reconcile(); await tick();
  f.select.value='translate-cs'; f.select.dispatchEvent(new Event('change')); await tick();
  finish({ok:true,json:async () => ({session:'old-original',language:'auto',translation:false})}); await tick();
  assert.equal(f.controller.session,'translation-session');
  assert.equal(f.controller.aiTrack.language,'cs');
  assert.ok(f.requests.some(r => r.url.endsWith('/old-original') && r.options.method==='DELETE'));
});

test('failed translation explicitly displays English without stopping transcription',async () => {
  const f=fixture(undefined,{mode:'translate-cs'}); f.controller.attach(17,f.hls); await tick();
  f.controller.cue({start:10,end:12,text:'English <caption>',translation_failed:true,language:'en'});
  assert.equal(f.controller.aiTrack.cues[0].text,'[EN] English &lt;caption&gt;');
  assert.match(f.status.textContent,/anglický přepis/);
  assert.equal(f.retry.hidden,false);
  assert.equal(f.controller.failed,false);
  assert.equal(f.controller.session,'translation-session');
});

test('translation uses buffered audio and schedules future cues without changing playback time',async () => {
  const f=fixture((url) => {
    if (url.includes('/audio?')) return new Response(
      JSON.stringify({type:'cue',start:16,end:18,text:'České titulky',language:'cs',translation:'cs'})+'\n',
      {headers:{'Content-Type':'application/x-ndjson'}});
  },{mode:'translate-cs'});
  f.controller.attach(17,f.hls); await tick();
  const blob=new Blob(['buffered MP4']);
  f.controller.fragments.set('future',{key:'future',start:16,end:18,blob});
  await f.controller.pump();
  const audio=f.requests.find(r => r.url.includes('/audio?'));
  assert.equal(audio.options.body,blob);
  assert.match(audio.url,/start=16&end=18/);
  assert.equal(f.video.currentTime,10);
  assert.equal(f.controller.aiTrack.cues[0].startTime,16);
  assert.equal(f.controller.aiTrack.cues[0].endTime,18);
  assert.equal(f.requests.every(r => r.url.startsWith('/api/captions/')),true);
});

test('a translated caption resumes after English fallback in the same session',async () => {
  const f=fixture(undefined,{mode:'translate-cs'}); f.controller.attach(17,f.hls); await tick();
  f.controller.cue({start:10,end:12,text:'English fallback',translation_failed:true});
  assert.equal(f.retry.hidden,false);
  f.video.currentTime=12;
  f.controller.cue({start:12,end:14,text:'Překlad pokračuje',translation:'cs'});
  assert.equal(f.controller.aiTrack.cues.at(-1).text,'Překlad pokračuje');
  assert.equal(f.status.textContent,'Překlad do češtiny');
  assert.equal(f.retry.hidden,true);
  assert.equal(f.controller.session,'translation-session');
});

test('detected languages choose Czech translation, Czech transcription or original-language fallback',async () => {
  const f=fixture(undefined,{mode:'translate-cs'}); f.controller.attach(17,f.hls); await tick();
  f.controller.cue({start:10,end:12,text:'Překlad z francouzštiny',source_language:'fr',translation:'cs'});
  assert.match(f.status.textContent,/Francouzština → čeština/);
  f.video.currentTime=12;
  f.controller.cue({start:12,end:14,text:'Český přepis',source_language:'cs',translation:'cs',translation_skipped:true});
  assert.equal(f.status.textContent,'Čeština · přepis bez překladu.');
  f.video.currentTime=14;
  f.controller.cue({start:14,end:16,text:'Svenska',source_language:'sv',translation_unsupported:true});
  assert.equal(f.controller.aiTrack.cues.at(-1).text,'[SV] Svenska');
  assert.match(f.status.textContent,/Švédština/);
  assert.equal(f.retry.hidden,true); // Unsupported language is not a model outage.
});

test('a failed session creation retries automatically with backoff',async () => {
  let attempts=0;
  const f=fixture((url,options) => {
    if (url==='/api/captions/sessions' && options.method==='POST' && ++attempts===1){
      return {ok:false,status:503};
    }
  },{mode:'translate-cs'});
  f.controller.attach(17,f.hls); await tick();
  assert.equal(f.controller.failed,true);
  assert.ok(f.controller.retryAt > performance.now());
  f.controller.reconcile(); await tick();
  assert.equal(attempts,1); // Reconcile must not hammer a failing backend.
  f.controller.retryAt=performance.now()-1; f.controller.reconcile(); await tick();
  assert.equal(attempts,2);
  assert.equal(f.controller.session,'translation-session');
  assert.equal(f.controller.failed,false);
});

test('an audio failure reconnects and resends buffered audio without changing channels',async () => {
  let attempts=0;
  const f=fixture(url => {
    if (url.includes('/audio?')){
      if (++attempts===1) return {ok:false,status:503};
      return new Response(JSON.stringify({type:'cue',start:16,end:18,text:'Obnovené titulky',translation:'cs'})+'\n');
    }
  },{mode:'translate-cs'});
  f.controller.attach(17,f.hls); await tick();
  f.controller.fragments.set('future',{key:'future',start:16,end:18,blob:new Blob(['MP4'])});
  await f.controller.pump();
  assert.equal(f.controller.failed,true);
  f.controller.retryAt=performance.now()-1; f.controller.reconcile(); await tick(); await tick();
  assert.equal(attempts,2);
  assert.equal(f.controller.channel,17);
  assert.equal(f.controller.aiTrack.cues[0].text,'Obnovené titulky');
  assert.equal(f.controller.retryCount,0);
});

test('audio progress timeout cannot leave captions permanently busy',async () => {
  const originalSetTimeout=globalThis.setTimeout, originalClearTimeout=globalThis.clearTimeout;
  const timers=new Map(); let token=0;
  globalThis.setTimeout=(fn,delay) => { const id=++token; timers.set(id,{fn,delay}); return id; };
  globalThis.clearTimeout=id => timers.delete(id);
  let f;
  try {
    f=fixture((url,options) => {
      if (url.includes('/audio?')) return {ok:true,status:200,body:{getReader(){ return {
        read(){ return new Promise((resolve,reject) => options.signal.addEventListener('abort',() =>
          reject(Object.assign(new Error('Aborted'),{name:'AbortError'})),{once:true})); }
      }; }}};
    },{mode:'translate-cs'});
    f.controller.attach(17,f.hls); await tick();
    f.controller.fragments.set('future',{key:'future',start:16,end:18,blob:new Blob(['MP4'])});
    const pumping=f.controller.pump(); await tick();
    const timer=Array.from(timers.values()).find(t => t.delay===45000);
    assert.ok(timer); timer.fn(); await pumping;
    assert.equal(f.controller.busy,false);
    assert.equal(f.controller.session,null);
    assert.equal(f.controller.failed,true);
    assert.ok(f.controller.retryAt > performance.now());
  } finally {
    f?.controller.stopASR();
    globalThis.setTimeout=originalSetTimeout; globalThis.clearTimeout=originalClearTimeout;
  }
});

test('audio cache retains nearby fragments when future prefetch fills the cache',async () => {
  const f=fixture(); f.video.paused=true; f.controller.attach(17,f.hls); await tick();
  const init=Uint8Array.from([0,0,0,8,102,116,121,112]);
  for (let number=0;number<24;number++){
    const frag={type:'main',level:0,cc:0,sn:number,start:10+number*6,duration:6};
    f.controller.parts.set(f.controller.fragmentKey(frag),{init,parts:[new Uint8Array(8)],bytes:8});
    f.controller.audioReady(frag);
  }
  const fragments=Array.from(f.controller.fragments.values());
  assert.equal(fragments.length,16);
  assert.ok(fragments.some(f => f.start===10));
  assert.ok(fragments.some(f => f.start===16));
  assert.ok(fragments.every(f => f.start<=100));
});

test('processed history cannot evict audio still needed for upcoming playback',async () => {
  const f=fixture(); f.video.paused=true; f.video.currentTime=30;
  f.controller.attach(17,f.hls); await tick();
  for (let number=0;number<30;number++){
    const frag={type:'main',level:0,cc:0,sn:number,start:number*2,duration:2};
    const key=f.controller.fragmentKey(frag);
    if (frag.start<30) f.controller.sent.add(key);
    f.controller.parts.set(key,{init:new Uint8Array(8),parts:[new Uint8Array(8)],bytes:8});
    f.controller.audioReady(frag);
  }
  const starts=Array.from(f.controller.fragments.values()).map(f => f.start);
  for (let start=30;start<60;start+=2) assert.ok(starts.includes(start),'upcoming audio '+start);
  assert.ok(starts.length<=16);
});

test('an hour of simulated playback survives periodic API failures with bounded caches',async () => {
  let requests=0, failures=0;
  const f=fixture(url => {
    if (!url.includes('/audio?')) return;
    if (++requests%41===0){ failures++; return {ok:false,status:503}; }
    const query=new URL(url,'http://localhost').searchParams;
    const start=Number(query.get('start')), end=Number(query.get('end'));
    const events=[];
    for (let at=start;at<end;at+=2) events.push(JSON.stringify({
      type:'cue',start:at,end:Math.min(at+2,end),text:'Pokračující překlad',translation:'cs',
    }));
    return new Response(events.join('\n')+'\n');
  },{mode:'translate-cs'});
  f.controller.attach(17,f.hls); await tick();
  for (let step=0;step<600;step++){
    f.video.currentTime=10+step*6;
    f.video.paused=true;
    for (let ahead=0;ahead<5;ahead++){
      const frag={type:'main',level:0,cc:0,sn:step+ahead,start:10+(step+ahead)*6,duration:6};
      f.controller.parts.set(f.controller.fragmentKey(frag),{
        init:new Uint8Array(8),parts:[new Uint8Array(8)],bytes:8,
      });
      f.controller.audioReady(frag);
    }
    f.video.paused=false;
    await f.controller.pump(); await tick();
    if (f.controller.failed){
      f.controller.retryAt=performance.now()-1; f.controller.reconcile(); await tick(); await tick();
    }
    assert.equal(f.controller.failed,false,'playback step '+step);
    assert.equal(f.controller.busy,false,'playback step '+step);
    assert.ok(f.controller.fragments.size<=16);
    assert.ok(f.controller.aiTrack.cues.length>0 && f.controller.aiTrack.cues.length<=150);
  }
  assert.ok(requests>=600 && failures>=10);
  assert.equal(f.controller.channel,17);
  assert.equal(f.controller.mode,'translate-cs');
});
