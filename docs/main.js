// config.js の downloadUrl が設定されていればダウンロードボタンを表示し、公開前の表示を外す。
// 未設定（または https:// で始まらない値）のあいだは、HTML に書かれた「公開準備中」の表示のまま。
(function () {
  var config = window.SITE_CONFIG || {};
  var url = (config.downloadUrl || "").trim();
  if (!/^https:\/\//i.test(url)) return;

  document.querySelectorAll("[data-download-link]").forEach(function (a) {
    a.href = url;
    a.hidden = false;
  });
  document.querySelectorAll("[data-ready]").forEach(function (el) { el.hidden = false; });
  document.querySelectorAll("[data-pending], [data-prerelease]").forEach(function (el) { el.remove(); });

  var info = [config.fileName, config.version && "v" + config.version, config.fileSize].filter(Boolean);
  document.querySelectorAll("[data-file-info]").forEach(function (el) { el.textContent = info.join("・"); });
})();
