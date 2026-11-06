const CACHE='perseo-pos-demo-v4';const SHELL=['/','/static/style.css','/static/pos-ui.css','/static/pos-ui-v3.css','/static/pos-adaptive.css','/static/theme-obsidian-titanium.css?v=1','/static/theme-obsidian-modules.css?v=2','/static/theme-luxury-motion.css?v=1','/static/theme-service-fast.css?v=2','/static/theme-pin-login.css?v=1','/static/demo.css?v=1','/static/perseo-brand.css?v=1','/static/perseo-dashboard.css?v=2','/static/assets/perseo-mark.svg','/static/features.js?v=3','/static/features.css?v=1','/static/cash-receipt.css?v=1','/static/app.js?v=32','/static/pos-ui-v3.js?v=7','/static/manifest.webmanifest','/static/assets/logo.webp'];self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)));self.skipWaiting()});self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim()});self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.pathname.startsWith('/api/'))return;e.respondWith(fetch(e.request).then(r=>{if(r.ok&&u.origin===location.origin){const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy))}return r}).catch(()=>caches.match(e.request)))})

// Mensajes del servidor en segundo plano: no dependen de la página abierta.
self.addEventListener('push',event=>{
  let data={};try{data=event.data?event.data.json():{}}catch{}
  const title=String(data.title||'PERSEO POS').slice(0,100);
  const body=String(data.body||'Nueva actualización').slice(0,250);
  const url=typeof data.url==='string'&&data.url.startsWith('/')&&!data.url.startsWith('//')?data.url:'/';
  event.waitUntil(self.registration.showNotification(title,{
    body,icon:'/static/assets/perseo-mark.svg',badge:'/static/assets/perseo-mark.svg',
    tag:String(data.tag||'perseo-notificacion'),renotify:true,requireInteraction:true,
    data:{url}
  }));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const target=new URL(event.notification.data?.url||'/',self.location.origin).href;
  event.waitUntil((async()=>{
    const clientsList=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    for(const client of clientsList){
      if(new URL(client.url).origin===self.location.origin){
        if('navigate' in client)await client.navigate(target);
        return client.focus();
      }
    }
    return self.clients.openWindow(target);
  })());
});
