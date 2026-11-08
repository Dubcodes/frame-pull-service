const root=document.querySelector('.person-overview');
const groupId=Number(root.dataset.groupId);
let group=null;
let candidates=[];

async function api(path,options){const response=await fetch(path,options);if(!response.ok)throw new Error(await response.text());return response.json()}
const text=value=>value??'Not supplied';
const localTime=value=>value?new Date(value).toLocaleString([],{dateStyle:'medium',timeStyle:'short',timeZone:'Pacific/Auckland'}):'Time unavailable';

function renderSummary(){
  document.querySelector('#person-name').textContent=group.name;
  document.querySelector('#breadcrumb-name').textContent=group.name;
  document.title=`Frame Pull — ${group.name}`;
  document.querySelector('#person-facts').innerHTML=`
    <div><dt>Interviews</dt><dd>${group.interview_count}</dd></div>
    <div><dt>Portrait candidates</dt><dd>${group.candidate_count}</dd></div>
    <div><dt>Location</dt><dd>${group.location_state==='conflict'?'Conflicting locations':text(group.track)}</dd></div>
    <div><dt>Roles</dt><dd>${group.roles.length?group.roles.join(', '):'Not supplied'}</dd></div>
    <div><dt>Review</dt><dd>${group.review_status}</dd></div>
    <div><dt>Export</dt><dd>${group.export_state}</dd></div>`;
  updatePortrait(group.holding_url);
}

function updatePortrait(url){
  const image=document.querySelector('#person-portrait'),placeholder=document.querySelector('#portrait-placeholder');
  if(url){image.src=url;image.hidden=false;placeholder.hidden=true}else{image.hidden=true;placeholder.hidden=false}
}

function renderInterviews(){
  document.querySelector('#interview-cards').innerHTML=group.interviews.map((item,index)=>{
    const context=item.calendar_context||{};
    return `<a class="interview-card" href="/review/${item.id}" data-interview-index="${index}">
      ${item.poster_url?`<img src="${item.poster_url}" alt="Interview ${item.id} poster">`:'<div class="portrait-frame">Poster unavailable</div>'}
      <div class="interview-copy"><h3>Interview ${item.id}</h3><div class="interview-meta">
        <span>${localTime(context.interview_time)}</span>
        <span>${text(context.resolved_track||item.track)} · ${context.race_number?`Race ${context.race_number}`:'Race unresolved'}</span>
        <span>${text(item.role)} · ${item.review_status}</span>
        <span>${item.candidate_count} candidates · ${Number(item.clip_duration||0).toFixed(1)}s clip</span>
      </div></div></a>`;
  }).join('');
}

function renderCandidates(){
  const region=document.querySelector('#group-candidates'),empty=document.querySelector('#no-candidates');
  empty.hidden=candidates.length!==0;
  region.hidden=candidates.length===0;
  region.innerHTML=candidates.map(item=>`<article class="candidate-card ${item.id===group.preferred_candidate_id?'selected':''}" data-candidate="${item.id}">
    <button class="candidate-select" data-select="${item.id}" title="Use candidate ${item.rank} as the current person portrait"><img src="${item.url}" alt="Portrait candidate ${item.rank}"></button>
    <div class="candidate-details"><strong>Interview ${item.interviewId} · Rank ${item.rank}</strong>${item.in_clip?'':'<div class="warning-badge">Outside current clip</div>'}
      <div class="candidate-actions"><span class="muted">${item.id===group.preferred_candidate_id?'Preferred':'Available'}</span><a href="/review/${item.interviewId}">Review interview</a></div>
    </div></article>`).join('');
}

async function chooseCandidate(candidateId){
  group=await api(`/api/appearance-groups/${groupId}/select-candidate`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({candidate_id:candidateId})});
  const selected=candidates.find(item=>item.id===candidateId);
  if(selected)updatePortrait(selected.url);
  renderCandidates();
  document.querySelector('#group-state').textContent='Preferred portrait saved';
}

async function load(){
  group=(await api('/api/appearance-groups')).find(item=>item.id===groupId);
  if(!group)throw new Error('Person group not found');
  candidates=(await Promise.all(group.interviews.map(async interview=>(await api(`/api/interviews/${interview.id}/candidates`)).map(item=>({...item,interviewId:interview.id}))))).flat();
  renderSummary();renderInterviews();renderCandidates();
}

document.querySelector('#group-candidates').addEventListener('click',event=>{const button=event.target.closest('[data-select]');if(button)chooseCandidate(Number(button.dataset.select))});
document.addEventListener('keydown',event=>{if(['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName)||event.target.isContentEditable)return;if(event.key.toLowerCase()==='b')location.href='/review';else if(/^[1-9]$/.test(event.key)){const member=group?.interviews[Number(event.key)-1];if(member)location.href=`/review/${member.id}`}else if(event.key==='?')alert('B back to review queue · 1–9 open interview')});
load().catch(error=>document.querySelector('#group-state').textContent=error.message);
