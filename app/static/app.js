const $=s=>document.querySelector(s);let chosen=null;
const drop=$('#drop'),file=$('#file'),browse=$('#browse'),filecard=$('#filecard'),fname=$('#fname'),fsize=$('#fsize'),analyze=$('#analyze'),progress=$('#progress'),fill=$('#fill'),pct=$('#pct'),pmsg=$('#pmsg'),pstatus=$('#pstatus'),results=$('#results'),grid=$('#grid');
browse.onclick=e=>{e.preventDefault();file.click()}; file.onchange=()=>pick(file.files[0]);
['dragenter','dragover'].forEach(x=>drop.addEventListener(x,e=>{e.preventDefault();drop.classList.add('drag')}));
['dragleave','drop'].forEach(x=>drop.addEventListener(x,e=>{e.preventDefault();drop.classList.remove('drag')}));
drop.addEventListener('drop',e=>pick(e.dataTransfer.files[0]));
function pick(f){if(!f)return;chosen=f;fname.textContent=f.name;fsize.textContent=(f.size/1024/1024).toFixed(1)+' MB';filecard.classList.remove('hidden');analyze.disabled=false;results.classList.add('hidden')}
$('#clear').onclick=()=>{chosen=null;file.value='';filecard.classList.add('hidden');analyze.disabled=true};
$('#another').onclick=()=>{results.classList.add('hidden');document.querySelector('#studio').scrollIntoView({behavior:'smooth'})};
analyze.onclick=async()=>{if(!chosen)return;analyze.disabled=true;progress.classList.remove('hidden');setp(4,'Uploading video…');const fd=new FormData();fd.append('video',chosen);fd.append('clip_length',$('#length').value);try{const r=await fetch('/api/jobs',{method:'POST',body:fd});const j=await r.json();if(!r.ok)throw Error(j.detail||'Upload failed');poll(j.job_id)}catch(e){fail(e.message)}};
function setp(n,msg){fill.style.width=n+'%';pct.textContent=n+'%';pmsg.textContent=msg}
async function poll(id){try{const r=await fetch('/api/jobs/'+id);const j=await r.json();setp(j.progress||0,j.message||'');pstatus.textContent=j.status==='done'?'Complete':'AI analysis';if(j.status==='done'){render(j.clips);analyze.disabled=false;return}if(j.status==='error'){fail(j.message);return}setTimeout(()=>poll(id),1000)}catch(e){fail(e.message)}}
function fail(m){pstatus.textContent='Could not process';setp(100,m);analyze.disabled=false}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function render(clips){grid.innerHTML=clips.map(c=>`<article class="clip"><video controls preload="metadata" src="${c.url}"></video><div class="clipbody"><span class="score">✦ ${c.viral_score}/100 VIRAL POTENTIAL</span><h3>${esc(c.title)}</h3><p class="why">Why AI picked it: ${esc(c.reason)}</p><div class="meta"><span>#${c.rank} pick</span><span>${c.start}s → ${c.end}s</span></div><a class="download" href="${c.url}" download>Download MP4</a></div></article>`).join('');results.classList.remove('hidden');results.scrollIntoView({behavior:'smooth'})}
