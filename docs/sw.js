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
  const url = (event.notification.data && event.notification.data.url) || ".";
  event.waitUntil(clients.openWindow(url));
});
