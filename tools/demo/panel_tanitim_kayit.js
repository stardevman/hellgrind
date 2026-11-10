const {chromium}=require('playwright');const fs=require('fs');
const REAL='11798c42-c45e-4a30-9887-1c8908831396';const B='http://localhost:8000/panel/';
const SCRIPT=fs.readFileSync('script.txt','utf8');
const W=ms=>new Promise(r=>setTimeout(r,ms));
const overlay=()=>{ if(document.getElementById('demo-cap')) return;
  const st=document.createElement('style');st.textContent=`#demo-cap{position:fixed;left:50%;top:22px;transform:translateX(-50%);z-index:2147483647;background:rgba(18,18,20,.92);color:#fff;border:1px solid #f07b3f;border-radius:12px;padding:14px 26px;font:600 26px/1.3 -apple-system,Helvetica,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.5);max-width:1500px;text-align:center;transition:opacity .4s;pointer-events:none}#demo-cap small{display:block;font-weight:400;font-size:19px;color:#ddd;margin-top:4px}#demo-cur{position:fixed;z-index:2147483647;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;background:rgba(240,123,63,.55);border:2px solid #fff;pointer-events:none;transition:transform .15s}`;
  document.body.appendChild(st);
  const c=document.createElement('div');c.id='demo-cur';document.body.appendChild(c);
  addEventListener('mousemove',e=>{c.style.left=e.clientX+'px';c.style.top=e.clientY+'px'},true);
  addEventListener('mousedown',()=>c.style.transform='scale(.6)',true);addEventListener('mouseup',()=>c.style.transform='',true);
  const cap=document.createElement('div');cap.id='demo-cap';cap.style.opacity=0;document.body.appendChild(cap);
  window.__cap=(t,s)=>{cap.innerHTML=t+(s?`<small>${s}</small>`:'');cap.style.opacity=t?1:0;};
  if(window.__lastCap) window.__cap(...window.__lastCap);
};
(async()=>{
 const b=await chromium.launch();
 const ctx=await b.newContext({viewport:{width:1920,height:1080},recordVideo:{dir:'raw',size:{width:1920,height:1080}}});
 await ctx.addInitScript(()=>{localStorage.setItem('hg-tour-done','1');});
 // auto-decline any consent / paid dialogs
 const p=await ctx.newPage(); p.on('dialog',d=>d.dismiss());
 let mx=960,my=540;
 const ensure=async()=>{await p.evaluate(overlay);await p.mouse.move(mx,my);};
 const cap=async(t,s='')=>{await ensure();await p.evaluate(([t,s])=>{window.__lastCap=[t,s];window.__cap&&window.__cap(t,s)},[t,s]);};
 const move=async(x,y,steps=25)=>{await p.mouse.move(x,y,{steps});mx=x;my=y;};
 const click=async(loc)=>{const bb=await loc.boundingBox();await move(bb.x+bb.width/2,bb.y+bb.height/2);await W(250);await loc.click();await W(400);};
 const hover=async(loc)=>{const bb=await loc.boundingBox();if(bb) await move(bb.x+bb.width/2,bb.y+bb.height/2,30);};
 const go=async(pg,proj=REAL)=>{await p.goto(`${B}?p=${proj}&page=${pg}`);await W(2500);await cap(...(await p.evaluate(()=>window.__lastCap||['',''])));};
 const safety=async()=>{ // refuse any consent dialog that appears
   const no=p.getByRole('button',{name:/Vazgeç|Reddet|Hayır|İptal/}); if(await no.count()) {await no.first().click().catch(()=>{});} };

 // ---- 0. Giriş
 await p.goto(`${B}?p=${REAL}&page=deliver`);await W(1500);
 await cap('Hellgrind Stüdyo · Üretim adımları','“Ponpon Kediye Ne Oldu? – Uzatılmış (sesli)” · 26 çekim · 1:56');await W(4500);

 // ---- 1. Yeni film
 await cap('1 · Yeni film oluştur','Proje yöneticisi › Yeni film');
 await click(p.locator('button.pb-btn[data-tour=home]'));await W(1500);
 await click(p.locator('.pm-card.new'));await W(1200);
 const title=p.locator('label.field:has-text("Proje adı") input');await click(title);
 await title.fill('');await title.pressSequentially('Ponpon Kediye Ne Oldu? - DEMO',{delay:60});
 const bud=p.locator('label.field:has-text("Sanal bütçe") input');await click(bud);await bud.fill('');await bud.pressSequentially('7',{delay:120});
 await click(p.locator('input[name=intensity][value=brief]'));await W(800);

 // ---- 2. Senaryo
 await cap('2 · Senaryoyu yaz','Her paragraf bir çekim; [KARAKTER: …] etiketleri varlıklara bağlanır');
 const ta=p.locator('textarea').first();await click(ta);await ta.fill('');
 const head=SCRIPT.slice(0,420);await ta.pressSequentially(head,{delay:14});
 await ta.fill(SCRIPT);await p.evaluate(()=>{const t=document.querySelector('textarea');t.scrollTop=t.scrollHeight;});await W(1800);
 await cap('2 · Senaryoyu işle ve üret','Ücretsiz mod: harcama sanal, hiçbir ücretli servis çağrılmaz');
 await p.screenshot({path:'shots/t_before_submit.png'}); if(process.env.TEST){await cap('(TEST: gönderim atlandı)');} else {await click(p.getByRole('button',{name:/Senaryoyu işle ve üret/}));await W(6000);await safety();await p.screenshot({path:'shots/t_after_submit.png'});}
 await cap('Senaryo 26 çekime ayrıştırıldı','Bundan sonrası: aynı senaryonun gerçek üretimi');await W(4000);

 // ---- 3. Görünüm: stil + varlıklar (gerçek proje, sadece bakış)
 await go('style');await cap('3 · Stil kılavuzu','Resimli kitap guaşı, sıcak kâğıt dokusu · tüm çekimlere aynı kurallar');
 await move(1300,300,40);await W(2500);await move(1700,500,40);await W(2500);
 await W(1000);
 // ---- 4. Ses
 await go('voice');await cap('4 · Anlatım (ElevenLabs)','21 replik · süreler çekimlere eşlendi · “Kayıtları buraya sürükle” ile kendi sesiniz');
 await move(700,300,40);await W(1500);await p.mouse.wheel(0,400);await move(900,500,30);await W(2500);await p.mouse.wheel(0,500);await W(2500);

 // ---- 5. Storyboard
 await go('board');await cap('5 · Storyboard','Kitap sayfalarından ilk kareler · zincir şeridi · yönetmen notları');
 await move(400,130,40);await W(1200);await move(1500,200,60);await W(1500);
 for(let i=0;i<6;i++){await p.mouse.wheel(0,350);await W(900);}
 await move(1000,600,30);await W(1500);
 const anim=p.getByRole('button',{name:'Animatik'}); if(await anim.count()){await click(anim);await cap('5 · Animatik','Kareler ve anlatım, video üretmeden önce zamanlamayı gösterir');await W(5000);}

 // ---- 6. Kurgu
 await go('edit');await cap('6 · Üretim ve kürasyon','Her çekim için adaylar üretilir, en iyisi “filmde” olarak seçilir');
 await W(1500);
 const shotsTab=p.getByRole('tab',{name:/Çekimler/}).or(p.getByRole('button',{name:/Çekimler/})).first();
 if(await shotsTab.count()){await click(shotsTab);await W(1200);}
 const play=p.getByRole('button',{name:/Oynat|Play/}).first(); 
 await move(405,634);await p.mouse.click(405,634);await W(4500);
 await cap('6 · Zaman çizelgesi','Görüntü, diyalog (anlatıcı) ve müzik izleri · 26/26 çekim tamam');
 for(const x of [300,620,980,1300,1650]){await move(x,765,30);await p.mouse.click(x,765);await W(1600);}
 await go('compare');await cap('7 · Adayları karşılaştır','Stil uyumu, tarif uyumu, anomaliler ve maliyet tek bakışta');await move(1500,1200>1080?700:700,40);await W(4000);
 await go('color');await cap('8 · Renk ve süreklilik','Önce / sonra · sahneler arası renk eşleştirme');await move(1100,600,40);await W(4000);

 // ---- 7. Teslim
 await go('deliver');await cap('9 · Teslim','26 sahne birleştirildi · EDL, OpenTimelineIO, çekim paketi, storyboard PDF');
 await W(2000);
 const v=p.locator('video').first(); if(await v.count()){await hover(v);await v.evaluate(e=>{e.muted=true;e.currentTime=20;e.play()});}
 await W(9000);
 await cap('Film hazır','Ponpon Kediye Ne Oldu? – Uzatılmış (sesli) · 1:56');await W(3500);
 await p.close();await ctx.close();await b.close();
})().catch(e=>{console.error(e);process.exit(1)});
