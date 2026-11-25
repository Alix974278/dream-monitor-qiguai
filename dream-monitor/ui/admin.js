'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  let csrfToken = ''; // 自动由本机后台返回，仅用于防止跨网站删除操作
  let users = [];
  let currentUid = ''; // 当前浏览器正在登录的账号，不能在这里删除自己
  let target = null;
  let deleting = false;
  const clean = (v) => String(v == null ? '' : v);
  const date = (value) => value ? new Date(Number(value) * 1000).toLocaleString('zh-CN', { hour12:false }) : '未记录';
  function message(msg, error=false) {
    const el=$('message'); el.hidden=false; el.className='message'+(error?' error':''); el.textContent=clean(msg);
  }
  async function api(path, options={}) {
    const headers = {...options.headers};
    if (options.method === 'POST' && path.startsWith('/api/admin/')) headers['X-Dream-Admin-CSRF'] = csrfToken;
    const res = await fetch(path,{...options,headers,cache:'no-store',credentials:'same-origin'});
    const payload = await res.json().catch(()=>({error:'服务器响应格式错误'}));
    if (!res.ok) throw new Error(payload.error || '请求失败 ('+res.status+')');
    return payload;
  }
  function el(tag, cls, content){const x=document.createElement(tag);if(cls)x.className=cls;if(content!==undefined)x.textContent=clean(content);return x;}
  function loadStats(){
    $('stat-total').textContent=users.length;
    $('stat-online').textContent=users.filter(u=>u.sessions>0).length;
    $('stat-tasks').textContent=users.filter(u=>u.task_active>0).length;
  }
  function render() {
    const query=$('filter').value.trim().toLowerCase();
    const shown=users.filter(u=>[u.name,u.uid,u.account,u.phone,u.school].some(v=>clean(v).toLowerCase().includes(query)));
    const box=$('user-list'); box.replaceChildren();
    if(!shown.length){box.append(el('p','empty',users.length?'没有匹配的账号':'后台没有保存的账号'));return;}
    for(const u of shown){
      const card=el('article','person');
      const name=el('div'); name.append(el('h4','',u.name), el('div','muted',u.school), el('div','id','ID：'+u.uid));
      const ident=el('div');ident.append(el('span','meta-label','登录账号 / 手机'),el('div','',u.account),el('div','muted','资料手机号：'+u.phone));
      const tasks=el('div');tasks.append(el('span','meta-label','已保存任务 / 未完成'),el('div','count',u.task_total+' / '+u.task_active),el('div','muted','最近使用：'+date(u.last_seen)));
      const action=el('div'); action.append(el('span','status'+(u.sessions?'':' off'),u.sessions?'有 '+u.sessions+' 个网页登录会话':'无有效网页登录会话'));
      action.append(el('div','', ''));
      if(currentUid && u.uid===currentUid){
        action.append(el('span','status self','当前登录账号'));
      }else{
        const btn=el('button','remove','删除账号');btn.type='button';btn.addEventListener('click',()=>openDelete(u));action.append(btn);
      }
      card.append(name,ident,tasks,action);box.append(card);
    }
  }
  async function refresh(){
    try{const data=await api('/api/admin/users');users=data.users||[];csrfToken=data.csrf_token||'';currentUid=String(data.current_uid||'');loadStats();render();}
    catch(err){message(err.message,true);}
  }
  function openDelete(u){
    if(deleting)return;
    target=u;
    $('target-info').textContent=u.name+' ｜ '+u.account+' ｜ 用户 ID：'+u.uid;
    $('delete-error').hidden=true;
    $('delete').disabled=false;
    $('delete').textContent='确认删除';
    $('confirm').showModal();
    $('cancel').focus();
  }
  $('refresh').addEventListener('click',refresh);
  $('filter').addEventListener('input',render);
  $('cancel').addEventListener('click',()=>{ if(!deleting) $('confirm').close(); });
  $('confirm').addEventListener('cancel',(event)=>{ if(deleting)event.preventDefault(); });
  $('confirm').addEventListener('close',()=>{ if(!deleting) target=null; });
  $('delete-form').addEventListener('submit',async(event)=>{
    event.preventDefault();
    if(!target||deleting)return;
    deleting=true;
    $('delete').disabled=true;
    $('cancel').disabled=true;
    $('delete').textContent='正在删除…';
    $('delete-error').hidden=true;
    const uid=target.uid,name=target.name;
    try{
      const res=await api('/api/admin/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uid,confirmed:true})});
      $('confirm').close();target=null;
      const pending=res.result.cache_files_pending?('；'+res.result.cache_files_pending+' 个缓存文件被占用，下次启动时自动删除'):'';
      message('已彻底删除 '+name+'（'+uid+'），同时删除定时任务 '+res.result.tasks_removed+' 条'+pending+'。CMD 窗口已打印更新后的用户数。');
      await refresh();
    }catch(err){
      $('delete-error').textContent='删除失败：'+err.message;
      $('delete-error').hidden=false;
      message(err.message,true);
    }finally{
      deleting=false;
      $('cancel').disabled=false;
      $('delete').textContent='确认删除';
      $('delete').disabled=false;
    }
  });
  refresh();
})();
