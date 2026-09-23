# PC管理センター 紹介ページ

アプリ本体とは別の、1 ページだけの静的サイトです。ビルドは不要で、このフォルダをそのまま公開できます。

```
website/
├─ index.html   本文
├─ style.css    見た目
├─ config.js    ダウンロード URL・版・ファイル名（公開時に変更するのはここだけ）
├─ main.js      config.js に URL があればボタンを表示する
├─ images/      アプリ画面（公開用テストデータで撮影）
└─ tools/       スクリーンショットの撮り直し用（公開不要）
```

## ローカルで確認する

`index.html` をブラウザで開けば表示できます。公開時と同じ条件で見る場合は、次を実行してから http://127.0.0.1:8000/ を開きます。

```
cd website
python -m http.server 8000 --bind 127.0.0.1
```

## 公開するとき

1. `config.js` の `downloadUrl` にインストーラーの URL（`https://` で始まるもの）を入れる。
   - 空のあいだは、上部に「公開前の確認用ページです」と出て、ダウンロードボタンは表示されません。
   - URL を入れると、その表示が消え、ページ上部と「入手方法」にボタンが出ます。
2. 版を上げたときは `config.js` の `version`・`fileName`・`fileSize`、`index.html` の「v0.5」の表記を更新する。
3. `tools/` はサーバーに置かなくてかまいません。

## スクリーンショットの撮り直し

画面に個人のファイル名・パスが写らないよう、架空のファイル（疎ファイル。見かけの容量だけでディスクはほぼ使わない）をスキャンして撮影します。撮影中にウィンドウを前面に出しません。

```
subst S: <空のフォルダ>
python tools/make_sample.py S:\Users\sample
python tools/capture.py <アプリのフォルダ> S:\Users\sample <出力フォルダ(絶対パス)>
subst S: /D
```

出力のうち、ページで使っているのは次の 5 枚です。

| 出力 | images/ のファイル名 |
|---|---|
| files.png | file-list.png |
| map.png | step1-map.png |
| files_filtered.png | step2-filter.png |
| detail_game.png | step3-detail.png |
| confirm_risky.png | step4-confirm.png |

画面上部の「ドライブ」の使用量だけは、実際のドライブではなくサンプル値（512 GB 中 318 GB）を表示しています。
