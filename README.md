# Blogger インデックス通知バッチ

## 概要

### 背景・目的

Bloggerで公開した記事のURLをGoogle Indexing APIに自動通知し、インデックス登録作業を自動化するためのバッチプログラムです。Firestoreで各URLの通知日時を管理し、結果をメールで通知します。

### 機能一覧

- Blogger APIから全記事URLを取得し、Firestoreに登録・管理します。
- Firestoreで各URLの通知日時を管理し、通知日時が古い順に指定件数だけURLを抽出してGoogle Indexing APIへ通知します。
- 通知結果をHTML形式でまとめ、指定アドレスへメール送信します。
- 各種設定値は環境変数で管理します。
- 標準出力に実行ログを出力します（ファイル出力はありません）。

## 必要な環境変数

| 変数名 | 用途 |
| ---- | ---- |
| BLOGGER_INDEX_REGIST_API_KEY | Blogger APIキー |
| BLOG_ID | 対象ブログのID |
| MAIL_FROM | 送信元メールアドレス（Gmail） |
| MAIL_PASSWORD | 送信元メールアドレスのアプリパスワード |
| MAIL_TO | 通知先メールアドレス |

## 変更可能な定数

main.py 冒頭で定義されています。用途に応じて値を調整してください。

| 定数名 | 用途 | デフォルト値 |
| ---- | ---- | ---- |
| BATCH_SIZE | 1回のバッチで通知するURLの最大件数 | 5 |
| SLEEP_SECONDS | 1件ごとに通知後の待機秒数（API制限緩和用） | 10 |
| SMTP_SERVER | メール送信に利用するSMTPサーバー | "smtp.gmail.com" |
| SMTP_PORT | SMTPサーバーのポート番号 | 587 |

## 入出力

### 入力

- Blogger APIから取得した記事URL
- Firestoreコレクション `url_notifications`

### 出力

- Google Indexing APIへの通知
- Firestoreの `last_sent` フィールド更新
- 通知結果のHTMLメール

## Firestore構造と通知日時の扱い

- コレクション名: `url_notifications`
- ドキュメントID: URLをBase64エンコードした文字列
- フィールド:
  - `url`: 記事URL
  - `last_sent`: 最終通知日時（サーバータイムスタンプ）

新規に取得したURLはFirestoreへ登録時、`last_sent` フィールドにサーバータイムスタンプ（現在時刻）が自動的に設定されます。

## 実行方法

Google Cloud Functions等のサーバーレス環境での実行を想定していますが、ローカル実行も可能です。

```bash
python main.py
```

※必要な環境変数を事前に設定してください。

## 処理概要

1. 環境変数から各種設定値を取得（未設定の場合はエラー出力し処理中断）
2. Google認証セッションを初期化
3. Blogger APIから記事URL一覧をFirestoreに登録（新規URLはlast_sentに現在時刻を設定）
4. Firestoreから通知日時が古い順に指定件数だけURLを抽出
5. Google Indexing APIへ通知し、結果をFirestoreに反映（APIエラー時は標準出力にエラー内容を出力し、処理は継続）
6. 全通知結果をHTMLメールで送信（メール送信失敗時は標準出力にエラー内容を出力する）

## メール通知

- 通知結果はHTML形式でメール送信されます。
- 件名は通知結果に応じて自動で変化します。
  - 失敗が1件でもあれば「【エラー】インデックス通知バッチ結果: 件数」
  - 全て成功の場合は「【完了】インデックス通知バッチ結果: 件数」
- メール本文のサンプルは `メール通知プレビュー.html` を参照してください。

## ライセンス

### 本プログラムのライセンス

- このプログラムはMITライセンスに基づいて提供されます。

### 使用ライブラリーのライセンス

| ライブラリ名 | バージョン | ライセンス |
| ---- | ---- | ---- |
| cachetools | 5.5.2 | Apache License 2.0 |
| certifi | 2025.6.15 | Mozilla Public License 2.0 |
| charset-normalizer | 3.4.2 | MIT License |
| google-api-core | 2.25.1 | Apache License 2.0 |
| google-api-python-client | 2.175.0 | Apache License 2.0 |
| google-auth | 2.40.3 | Apache License 2.0 |
| google-auth-httplib2 | 0.2.0 | Apache License 2.0 |
| googleapis-common-protos | 1.70.0 | Apache License 2.0 |
| httplib2 | 0.22.0 | MIT License |
| idna | 3.10 | BSD 3-Clause License |
| proto-plus | 1.26.1 | Apache License 2.0 |
| protobuf | 6.31.1 | BSD 3-Clause License |
| pyasn1 | 0.6.1 | BSD 2-Clause License |
| pyasn1_modules | 0.4.2 | BSD 2-Clause License |
| pyparsing | 3.2.3 | MIT License |
| requests | 2.32.4 | Apache License 2.0 |
| rsa | 4.9.1 | Apache License 2.0 |
| uritemplate | 4.2.0 | Apache License 2.0 |
| urllib3 | 2.5.0 | MIT |

## 開発詳細

### 開発環境

- VSCode バージョン 1.101.2
- Python 3.12.10

## 改訂履歴

| バージョン | 日付 | 内容 |
| ----- | ---------- | -------------- |
| 1.0.0 | 2025-07-07 | 初版リリース |
