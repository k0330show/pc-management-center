// ダウンロード案内の設定。公開時はここだけを書き換える。
//
// downloadUrl が空のあいだは「公開準備中」と表示し、ダウンロードボタンは出さない。
// https:// で始まる URL を入れると、ページ上部の案内とダウンロード欄にボタンが表示される。
window.SITE_CONFIG = {
  downloadUrl: "https://github.com/k0330show/pc-management-center/releases/download/V0.5/PC._Setup_0.5.exe",

  // インストーラーの情報（版を上げたときに合わせて更新する）
  version: "0.5",
  fileName: "PC管理センター_Setup_0.5.exe",
  fileSize: "約 12 MB",
};
