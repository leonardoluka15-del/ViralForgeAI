const $=s=>document.querySelector(s);let chosen=null;let sourceMode='file';let currentJobId=null;
const drop=$('#drop'),file=$('#file'),browse=$('#browse'),filecard=$('#filecard'),fname=$('#fname'),fsize=$('#fsize'),analyze=$('#analyze'),progress=$('#progress'),fill=$('#fill'),pct=$('#pct'),pmsg=$('#pmsg'),pstatus=$('#pstatus'),results=$('#results'),grid=$('#grid'),videoUrl=$('#video-url'),tabFile=$('#tab-file'),tabLink=$('#tab-link'),fileSource=$('#file-source'),linkSource=$('#link-source');
browse.onclick=e=>{e.preventDefault();file.click()}; file.onchange=()=>pick(file.files[0]);
tabFile.onclick=()=>setSource('file'); tabLink.onclick=()=>setSource('link');
videoUrl.addEventListener('input',()=>{analyze.disabled=!videoUrl.value.trim()});
function setSource(mode){sourceMode=mode;const isFile=mode==='file';tabFile.classList.toggle('active',isFile);tabLink.classList.toggle('active',!isFile);fileSource.classList.toggle('hidden',!isFile);linkSource.classList.toggle('hidden',isFile);filecard.classList.toggle('hidden',!isFile||!chosen);analyze.disabled=isFile?!chosen:!videoUrl.value.trim();results.classList.add('hidden')}
['dragenter','dragover'].forEach(x=>drop.addEventListener(x,e=>{e.preventDefault();drop.classList.add('drag')}));
['dragleave','drop'].forEach(x=>drop.addEventListener(x,e=>{e.preventDefault();drop.classList.remove('drag')}));
drop.addEventListener('drop',e=>pick(e.dataTransfer.files[0]));
function pick(f){if(!f)return;chosen=f;fname.textContent=f.name;fsize.textContent=(f.size/1024/1024).toFixed(1)+' MB';filecard.classList.remove('hidden');analyze.disabled=false;results.classList.add('hidden')}
$('#clear').onclick=()=>{chosen=null;file.value='';filecard.classList.add('hidden');analyze.disabled=true};
$('#another').onclick=()=>{results.classList.add('hidden');document.querySelector('#studio').scrollIntoView({behavior:'smooth'})};
analyze.onclick=async()=>{if(sourceMode==='file'&&!chosen)return;if(sourceMode==='link'&&!videoUrl.value.trim())return;analyze.disabled=true;progress.classList.remove('hidden');setp(4,sourceMode==='file'?'Uploading video…':'Fetching video from link…');const fd=new FormData();if(sourceMode==='file')fd.append('video',chosen);else fd.append('video_url',videoUrl.value.trim());fd.append('clip_length',$('#length').value);try{const r=await fetch('/api/jobs',{method:'POST',body:fd});const j=await r.json();if(!r.ok)throw Error(j.detail||'Upload failed');currentJobId=j.job_id;poll(j.job_id)}catch(e){fail(e.message)}};
function setp(n,msg){fill.style.width=n+'%';pct.textContent=n+'%';pmsg.textContent=msg}
async function poll(id){try{const r=await fetch('/api/jobs/'+id);const raw=await r.text();let j;try{j=JSON.parse(raw)}catch(_){throw Error(raw||'The processing server restarted while analyzing the video. Please retry.')}setp(j.progress||0,j.message||'');pstatus.textContent=j.status==='done'?'Complete':'AI analysis';if(j.status==='done'){render(j.clips);analyze.disabled=false;return}if(j.status==='error'){fail(j.message);return}setTimeout(()=>poll(id),1500)}catch(e){fail(e.message)}}
function fail(m){pstatus.textContent='Could not process';setp(100,m);analyze.disabled=false}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function render(clips){grid.innerHTML=clips.map(c=>`<article class="clip"><video controls preload="metadata" src="${c.url}"></video><div class="clipbody"><span class="score">✦ ${c.viral_score}/100 VIRAL POTENTIAL</span><h3>${esc(c.title)}</h3><p class="why">Why AI picked it: ${esc(c.reason)}</p><div class="meta"><span>#${c.rank} pick</span><span>${c.start}s → ${c.end}s</span></div><div class="clipactions"><a class="download" href="${c.url}" download>Download MP4</a><button class="ytpublish" onclick="publishYT(${c.rank},this)">Upload privately to YouTube</button></div></div></article>`).join('');results.classList.remove('hidden');results.scrollIntoView({behavior:'smooth'})}

async function checkYT(){const s=$('#yt-status'),d=$('#yt-detail');if(!s)return;try{const r=await fetch('/api/youtube/status');const j=await r.json();if(j.authorized){s.textContent='● '+(j.channel_title||'YouTube connected');s.classList.add('ok');d.textContent='Authorized • '+(j.subscribers||'0')+' subscribers'}else if(j.configured){s.textContent='YouTube needs attention';d.textContent=j.error||'Authorization check failed'}else{s.textContent='YouTube not configured';d.textContent='Add credentials in Render'}}catch(e){s.textContent='YouTube status unavailable';d.textContent=e.message}}
async function publishYT(rank,btn){if(!currentJobId)return alert('Process a video first.');const old=btn.textContent;btn.disabled=true;btn.textContent='Uploading…';const fd=new FormData();fd.append('job_id',currentJobId);fd.append('rank',rank);fd.append('privacy','private');try{const r=await fetch('/api/youtube/upload',{method:'POST',body:fd});const j=await r.json();if(!r.ok)throw Error(j.detail||'Upload failed');btn.textContent='Uploaded ✓';btn.classList.add('done');alert('Uploaded privately to your YouTube channel. Video ID: '+j.video_id)}catch(e){btn.disabled=false;btn.textContent=old;alert(e.message)}}
checkYT();

const trendGrid=$('#trend-grid'),trendStatus=$('#trend-status'),trendRegion=$('#trend-region'),refreshTrends=$('#refresh-trends');
function fmt(n){return new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(Number(n||0))}
async function loadTrends(){
  if(!trendGrid||!trendStatus)return;
  trendStatus.textContent='Loading current YouTube trends…';
  refreshTrends && (refreshTrends.disabled=true);
  try{
    const r=await fetch('/api/trends?region='+(trendRegion?.value||'US')+'&max_results=24');
    const raw=await r.text(); let j;
    try{j=JSON.parse(raw)}catch(_){throw Error(raw||'Trend Scout returned an invalid response')}
    if(!r.ok)throw Error(j.detail||'Trend Scout failed');
    trendGrid.innerHTML=(j.items||[]).map(t=>`<article class="trend-card">
      <a class="trend-thumb" href="${t.url}" target="_blank" rel="noopener"><img src="${t.thumbnail||''}" alt=""></a>
      <div class="trend-body">
        <div class="trend-top"><span class="trend-rank">#${t.rank}</span><span class="trend-score">✦ ${t.trend_score}/100</span></div>
        <h3>${esc(t.title||'Untitled')}</h3>
        <p class="trend-channel">${esc(t.channel||'Unknown channel')}</p>
        <div class="trend-metrics"><span><b>${fmt(t.views)}</b> views</span><span><b>${fmt(t.views_per_hour)}</b>/hr</span><span><b>${t.age_hours}h</b> old</span></div>
        <a class="trend-open" href="${t.url}" target="_blank" rel="noopener">Open source video ↗</a>
      </div>
    </article>`).join('');
    trendStatus.textContent=`${j.count||0} current trends ranked by momentum`;
  }catch(e){
    trendStatus.textContent='Could not load trends: '+e.message;
    trendGrid.innerHTML='';
  }finally{
    refreshTrends && (refreshTrends.disabled=false);
  }
}
refreshTrends && (refreshTrends.onclick=loadTrends);
trendRegion && (trendRegion.onchange=loadTrends);
loadTrends();
