// Runs inside the app's pages in the browser version of Broadsheet (see /web-files/shell.js).
(function () {
  "use strict";
  // Other sites refuse to load inside the app's frame, so open them in a new tab.
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.target) return;
    if (new URL(a.href, location.href).origin !== location.origin) {
      a.target = "_blank";
      a.rel = "noopener";
    }
  }, true);

  // Settings › This browser › Download a backup.
  document.addEventListener("click", async function (e) {
    var button = e.target.closest && e.target.closest("#backup-download");
    if (!button) return;
    e.preventDefault();
    button.disabled = true;
    try {
      var r = await fetch("/browser/export");
      if (!r.ok) throw new Error(r.status);
      var url = URL.createObjectURL(await r.blob());
      var a = document.createElement("a");
      a.href = url;
      a.download = "broadsheet-backup-" + new Date().toISOString().slice(0, 10) + ".zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
    } catch (err) {
      alert("Couldn't make the backup (" + err.message + ").");
    } finally {
      button.disabled = false;
    }
  });
})();
