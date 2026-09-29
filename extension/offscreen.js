// Turns the zip the service worker left in IndexedDB into a blob: URL that
// chrome.downloads can save, then reports it back. Lives until the download finishes;
// the worker closes this document afterwards, which revokes the URL.

function readZip() {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open("hatch", 1);
    open.onupgradeneeded = () => open.result.createObjectStore("blobs");
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const req = open.result.transaction("blobs").objectStore("blobs").get("zip");
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    };
  });
}

readZip()
  .then((blob) => {
    if (!(blob instanceof Blob)) throw new Error("zip not found in storage");
    chrome.runtime.sendMessage({ type: "offscreen:ready", url: URL.createObjectURL(blob) });
  })
  .catch((e) => chrome.runtime.sendMessage({ type: "offscreen:ready", error: String(e.message || e) }));
