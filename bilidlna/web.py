"""网页管理端：扫码登录、搜索词、手动添加、清晰度设置。"""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bili DLNA</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#18191c;--sub:#61666d;--line:#e3e5e7;--pink:#fb7299;--blue:#00aeec}
@media (prefers-color-scheme:dark){:root{--bg:#17181a;--card:#222326;--fg:#e7e9eb;--sub:#9499a0;--line:#34363a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:22px;margin:0 0 4px}h1 b{color:var(--pink)}.sub{color:var(--sub);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-top:16px}
.card h2{font-size:16px;margin:0 0 10px}
input,select{font:inherit;color:inherit;background:transparent;border:1px solid var(--line);border-radius:8px;padding:7px 10px;min-width:0}
button{font:inherit;border:0;border-radius:8px;padding:7px 14px;background:var(--pink);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--sub);border:1px solid var(--line)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.row input{flex:1}
ul{list-style:none;margin:10px 0 0;padding:0}li{display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}
li span{flex:1}li a{color:var(--blue);font-size:13px;white-space:nowrap}
#qr{background:#fff;padding:10px;display:inline-block;border-radius:8px;margin-top:10px}
.ok{color:#2a9d5c}.warn{color:#e5763a}code{background:var(--bg);padding:1px 6px;border-radius:4px}
#msg{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#000c;color:#fff;padding:8px 16px;border-radius:8px;display:none}
</style></head><body><main>
<h1><b>Bilibili</b> DLNA 桥接</h1>
<div class="sub">在 Quest 的 4XVR / Skybox 里打开「网络 / DLNA」，选择 <b id="devname"></b> 即可浏览播放。</div>

<div class="card"><h2>账号</h2><div id="acct">加载中…</div><div id="qrbox" style="display:none"><div id="qr"></div><div id="qrstat" class="sub"></div></div></div>

<div class="card"><h2>手动添加（BV号 / 视频链接 / b23.tv 短链 / 直播间号）</h2>
<div class="row"><input id="addtxt" placeholder="例如 BV1xx411c7mD 或 https://live.bilibili.com/21452505"><button onclick="add()">添加</button></div>
<ul id="added"></ul></div>

<div class="card"><h2>搜索（会出现在播放器的「搜索」文件夹里）</h2>
<div class="row"><input id="kw" placeholder="关键词，例如 VR180 旅行"><button onclick="addSearch()">添加搜索</button></div>
<ul id="searches"></ul></div>

<div class="card"><h2>播放设置</h2>
<div class="row">最高清晰度 <select id="max_qn">
<option value="127">8K</option><option value="126">杜比视界</option><option value="125">HDR</option>
<option value="120">4K</option><option value="116">1080P60</option><option value="112">1080P+</option>
<option value="80">1080P</option><option value="64">720P</option><option value="32">480P</option></select>
视频编码 <select id="codec"><option value="hevc">HEVC（推荐）</option><option value="avc">AVC（兼容性最好）</option><option value="av1">AV1（Quest 3 可硬解）</option></select>
直播编码 <select id="live_codec"><option value="avc">AVC</option><option value="hevc">HEVC</option></select>
<label><input type="checkbox" id="vr_tags"> 自动识别 VR/3D 并加文件名标签</label>
<button onclick="saveSettings()">保存</button></div>
<div class="sub" style="margin-top:8px">4K / 1080P 需要登录，4K 以上与 1080P60 需要大会员。Quest 屏幕为 SDR，HDR / 杜比视界可能偏灰。</div></div>

<div class="card"><h2>直接测试</h2><div class="sub">用电脑上的 VLC 打开下面的地址可以先验证播放：<br><code id="testurl"></code></div></div>
</main><div id="msg"></div>
<script>
const $=id=>document.getElementById(id);
function toast(t){const m=$('msg');m.textContent=t;m.style.display='block';clearTimeout(m._t);m._t=setTimeout(()=>m.style.display='none',2500)}
async function api(p,body){const r=await fetch(p,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const j=await r.json();if(j.error)throw new Error(j.error);return j}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){
  const s=await api('/api/status');
  $('devname').textContent=s.name;
  $('testurl').textContent=s.base+'/vod/BV1GJ411x7h7/0/test.ts';
  $('acct').innerHTML=s.logged_in?`<span class="ok">已登录：${esc(s.uname)}${s.vip?'（大会员）':''}</span> <button class="ghost" onclick="logout()">退出登录</button>`
    :`<span class="warn">未登录（最高 480P）</span> <button onclick="login()">扫码登录</button>`;
  $('added').innerHTML=s.added.map((a,i)=>`<li><span>${a.type=='live'?'🔴 直播':'🎬'} ${esc(a.title||a.id)} <span class="sub">${esc(a.id)}</span></span><a href="#" onclick="del('/api/added/del',${i});return false">删除</a></li>`).join('');
  $('searches').innerHTML=s.searches.map((k,i)=>`<li><span>🔍 ${esc(k)}</span><a href="#" onclick="del('/api/search/del',${i});return false">删除</a></li>`).join('');
  for(const k of ['max_qn','codec','live_codec'])$(k).value=String(s.settings[k]);
  $('vr_tags').checked=!!s.settings.vr_tags;
}
async function login(){
  const {url,key}=await api('/api/qr',{});
  $('qrbox').style.display='block';$('qr').innerHTML='';new QRCode($('qr'),{text:url,width:180,height:180});
  $('qrstat').textContent='请用哔哩哔哩手机 App 扫码';
  const t=setInterval(async()=>{try{const r=await api('/api/qr/poll?key='+encodeURIComponent(key));
    $('qrstat').textContent=r.text;
    if(r.code===0){clearInterval(t);$('qrbox').style.display='none';toast('登录成功');load()}
    else if(r.code===86038){clearInterval(t)}}catch(e){clearInterval(t);$('qrstat').textContent=e.message}},1500);
}
async function logout(){await api('/api/logout',{});load()}
async function add(){try{const r=await api('/api/add',{text:$('addtxt').value});$('addtxt').value='';toast('已添加：'+(r.title||r.id));load()}catch(e){toast(e.message)}}
async function addSearch(){const kw=$('kw').value.trim();if(!kw)return;await api('/api/search',{kw});$('kw').value='';load()}
async function del(p,i){await api(p,{i});load()}
async function saveSettings(){await api('/api/settings',{max_qn:+$('max_qn').value,codec:$('codec').value,live_codec:$('live_codec').value,vr_tags:$('vr_tags').checked});toast('已保存，新打开的视频生效')}
$('addtxt').addEventListener('keydown',e=>e.key=='Enter'&&add());$('kw').addEventListener('keydown',e=>e.key=='Enter'&&addSearch());
load().catch(e=>toast(e.message));
</script></body></html>
"""
