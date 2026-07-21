self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data.json(); } catch (e) { data = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "Seat Scanner", {
    body: data.body || "",
    tag: data.tag,
    icon: "icon-192.png",
    badge: "icon-192.png",
    data: { url: data.url },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const raw = (event.notification.data && event.notification.data.url) || ".";
  // Resolve relative payload URLs (e.g. "open.html?...") against the SW scope
  // so the notification opens our own booking hand-off page on this origin.
  const url = new URL(raw, self.registration.scope).href;
  event.waitUntil((async () => {
    const all = await clients.matchAll({ type: "window", includeUncontrolled: true });
    // Already showing the target page → just focus it.
    for (const c of all) {
      if (c.url === url && "focus" in c) return c.focus();
    }
    // A PWA window is open but elsewhere: iOS standalone apps often won't honor
    // openWindow, so focus the existing window and steer it to the target —
    // via navigate(), falling back to a postMessage the page acts on.
    for (const c of all) {
      if ("focus" in c) {
        try { await c.focus(); } catch (e) { /* ignore */ }
        if ("navigate" in c) { try { return await c.navigate(url); } catch (e) { /* fall through */ } }
        c.postMessage({ type: "navigate", url });
        return;
      }
    }
    return clients.openWindow(url);
  })());
});
