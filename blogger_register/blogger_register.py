"""blogger_register.py

Copyright (c) 2026 Shinoryo
Licensed under the MIT License
"""

import base64
import gzip
import os
import smtplib
import time
from datetime import UTC, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, TypedDict
from urllib.parse import urlparse

import google.auth
import requests
from defusedxml import ElementTree as DefusedElementTree
from google.auth.transport.requests import AuthorizedSession
from google.cloud import firestore

# 定数定義
SCOPES: list[str] = ["https://www.googleapis.com/auth/indexing"]
ENDPOINT: str = "https://indexing.googleapis.com/v3/urlNotifications:publish"
BATCH_SIZE: int = 5
SLEEP_SECONDS: int = 10  # API制限緩和のための待機時間(秒)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
HTTP_STATUS_OK = 200
FIRESTORE_BATCH_LIMIT = 500
INITIAL_TIMESTAMP = datetime(1970, 1, 1, tzinfo=UTC)  # 新規URL用の初期タイムスタンプ
MIN_NOTIFY_INTERVAL_DAYS: int = 0  # 通知間隔の最小日数(0以下=制限なし)
MAX_SITEMAP_COUNT = 100
USER_AGENT = "BloggerRegister/1.0 (sitemap fetcher)"

db = firestore.Client()


class EnvVars(TypedDict):
    sitemap_url: str
    mail_from: str
    mail_password: str
    mail_to: str


class NotificationResult(TypedDict):
    url: str
    status: str
    http_status: int
    message: str


def get_env_vars() -> EnvVars:
    """必要な環境変数を取得し、存在しない場合は例外を投げる。

    Returns:
        EnvVars: 必要な環境変数を格納した辞書

    Raises:
        EnvironmentError: 必須環境変数が未設定の場合
    """
    env = {
        "sitemap_url": os.environ.get("SITEMAP_URL"),
        "mail_from": os.environ.get("MAIL_FROM"),
        "mail_password": os.environ.get("MAIL_PASSWORD"),
        "mail_to": os.environ.get("MAIL_TO"),
    }
    for key, value in env.items():
        if not value:
            message = f"環境変数 {key.upper()} が設定されていません。"
            raise OSError(message)
    return env


def encode_doc_id(url: str) -> str:
    """URLをBase64 URLセーフエンコードしてFirestoreのドキュメントIDに変換する。

    Args:
        url (str): エンコード対象のURL

    Returns:
        str: エンコード後の文字列
    """
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8")


def get_pending_url_docs(batch_size: int) -> list[firestore.DocumentSnapshot]:
    """Firestoreから送信が古い、もしくは未送信のURL通知ドキュメントを指定数取得する。

    MIN_NOTIFY_INTERVAL_DAYS定数が0より大きい場合、
    last_sentが指定日数以内のURLは除外される。

    Args:
        batch_size (int): 取得するドキュメント数の上限

    Returns:
        List[firestore.DocumentSnapshot]: 取得したドキュメントリスト
    """
    # MIN_NOTIFY_INTERVAL_DAYSが0以下の場合は制限なし
    if MIN_NOTIFY_INTERVAL_DAYS <= 0:
        docs = (
            db.collection("url_notifications")
            .order_by("last_sent")
            .limit(batch_size)
            .stream()
        )
        return list(docs)

    # MIN_NOTIFY_INTERVAL_DAYSが1以上の場合、
    # 指定日数以前のlast_sentを持つドキュメントのみ取得
    cutoff_time = datetime.now(tz=UTC) - timedelta(days=MIN_NOTIFY_INTERVAL_DAYS)
    print(
        f"MIN_NOTIFY_INTERVAL_DAYS={MIN_NOTIFY_INTERVAL_DAYS}: {cutoff_time.isoformat()}以前のURLのみ取得します。",  # noqa: E501
    )

    docs = (
        db.collection("url_notifications")
        .where("last_sent", "<=", cutoff_time)
        .order_by("last_sent")
        .limit(batch_size)
        .stream()
    )
    return list(docs)


def update_last_sent_timestamps(
    doc_refs: list[firestore.DocumentReference],
) -> None:
    """Firestoreのドキュメントのlast_sentフィールドをバッチで更新する。

    Args:
        doc_refs (list[firestore.DocumentReference]): 更新対象のドキュメント参照
    """
    if not doc_refs:
        return
    for start in range(0, len(doc_refs), FIRESTORE_BATCH_LIMIT):
        batch = db.batch()
        for doc_ref in doc_refs[start : start + FIRESTORE_BATCH_LIMIT]:
            batch.update(doc_ref, {"last_sent": firestore.SERVER_TIMESTAMP})
        batch.commit()


def build_last_sent_cache(page_size: int) -> dict[str, bool]:
    """Firestoreからlast_sentの有無をキャッシュする。

    Args:
        page_size (int): 1回の取得件数

    Returns:
        dict[str, bool]: ドキュメントIDごとのlast_sent有無
    """
    has_last_sent: dict[str, bool] = {}
    base_query = (
        db.collection("url_notifications")
        .order_by("__name__")
        .limit(page_size)
    )
    last_doc = None
    while True:
        query = base_query.start_after(last_doc) if last_doc else base_query
        docs = list(query.stream())
        if not docs:
            break
        for doc in docs:
            has_last_sent[doc.id] = doc.get("last_sent") is not None
        last_doc = docs[-1]
    return has_last_sent


def commit_pending_batch(
    batch: firestore.WriteBatch,
    pending_doc_ids: set[str],
    has_last_sent: dict[str, bool],
) -> firestore.WriteBatch:
    """バッチ書き込みを実行してキャッシュを更新する。

    pending_doc_ids が空の場合は書き込みを行わず終了する。
    batch.commit() 後に pending_doc_ids と has_last_sent を更新する。

    Args:
        batch (firestore.WriteBatch): 書き込みバッチ
        pending_doc_ids (set[str]): バッチ対象ドキュメントID
        has_last_sent (dict[str, bool]): last_sentの存在キャッシュ

    Returns:
        firestore.WriteBatch: 次のバッチ
    """
    if not pending_doc_ids:
        return batch
    batch.commit()
    for doc_id in pending_doc_ids:
        has_last_sent[doc_id] = True
    pending_doc_ids.clear()
    return db.batch()


def send_indexing_notification(
    url: str,
    authed_session: AuthorizedSession,
) -> tuple[bool, int, str]:
    """インデックス登録APIにURL更新通知を送信する。

    Args:
        url (str): 通知対象のURL
        authed_session (AuthorizedSession): 認証済みHTTPセッション

    Returns:
        Tuple[bool, int, str]: (送信成功か, HTTPステータスコード, レスポンステキスト)
    """
    payload = {"url": url, "type": "URL_UPDATED"}
    response = authed_session.post(ENDPOINT, json=payload)
    success = response.status_code == HTTP_STATUS_OK
    if success:
        print(f"通知送信成功: URL={url} ステータスコード={response.status_code}")
    else:
        print(
            f"通知送信失敗: URL={url} ステータスコード={response.status_code} レスポンス={response.text}",  # noqa: E501
        )
    return success, response.status_code, response.text


def decode_sitemap_content(content: bytes, url: str) -> bytes:
    """Sitemapコンテンツを必要に応じてデコードする。

    Args:
        content (bytes): 取得したSitemapのバイト列
        url (str): SitemapのURL

    Returns:
        bytes: デコード済みのSitemap
    """
    if url.lower().endswith(".gz"):
        return gzip.decompress(content)
    return content


def ensure_https_url(url: str) -> None:
    """Sitemap URLがHTTPSかを検証する。

    Args:
        url (str): 検証対象のURL

    Raises:
        ValueError: HTTPSではない場合
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        message = f"HTTPS以外のサイトマップURLは許可されていません: {url}"
        raise ValueError(message)


def extract_sitemap_entries(content: bytes) -> tuple[list[str], list[str]]:
    """Sitemap XMLからURLと子Sitemap URLを抽出する。

    Args:
        content (bytes): XMLコンテンツ

    Returns:
        tuple[list[str], list[str]]: (URLリスト, 子Sitemap URLリスト)
    """
    root = DefusedElementTree.fromstring(content)
    namespace = ""
    if root.tag.startswith("{"):
        namespace = root.tag.partition("}")[0] + "}"

    if root.tag.endswith("urlset"):
        urls = [
            loc.text.strip()
            for loc in root.findall(f".//{namespace}url/{namespace}loc")
            if loc.text
        ]
        return urls, []
    if root.tag.endswith("sitemapindex"):
        sitemap_urls = [
            loc.text.strip()
            for loc in root.findall(f".//{namespace}sitemap/{namespace}loc")
            if loc.text
        ]
        return [], sitemap_urls
    return [], []


def fetch_sitemap_urls(sitemap_url: str) -> list[str]:
    """サイトマップからURL一覧を取得する。

    Args:
        sitemap_url (str): 取得対象のサイトマップURL

    Returns:
        list[str]: 取得したURL一覧
    """
    ensure_https_url(sitemap_url)
    pending_sitemaps = [sitemap_url]
    visited_sitemaps: set[str] = set()
    seen_urls: set[str] = set()
    collected_urls: list[str] = []

    while pending_sitemaps:
        if len(visited_sitemaps) >= MAX_SITEMAP_COUNT:
            message = "サイトマップ取得数が上限を超えたため処理を中断します。"
            raise RuntimeError(message)
        current_url = pending_sitemaps.pop(0)
        if current_url in visited_sitemaps:
            continue
        ensure_https_url(current_url)
        visited_sitemaps.add(current_url)
        try:
            response = requests.get(
                current_url,
                timeout=30,
                verify=True,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
        except (requests.RequestException, ValueError) as exc:
            message = f"サイトマップの取得に失敗しました: {current_url}"
            raise RuntimeError(message) from exc
        content = decode_sitemap_content(response.content, current_url)
        try:
            urls, sitemap_urls = extract_sitemap_entries(content)
        except (DefusedElementTree.ParseError, ValueError) as exc:
            message = f"サイトマップXMLの解析に失敗しました: {current_url}"
            raise RuntimeError(message) from exc
        for url in urls:
            if url not in seen_urls:
                seen_urls.add(url)
                collected_urls.append(url)
        for child_url in sitemap_urls:
            if (
                child_url not in visited_sitemaps
                and child_url not in pending_sitemaps
            ):
                pending_sitemaps.append(child_url)

    return collected_urls


def register_sitemap_urls_to_firestore(sitemap_url: str) -> None:
    """サイトマップからURL一覧を取得し、Firestoreに登録する。

    Args:
        sitemap_url (str): サイトマップURL
    """
    has_last_sent = build_last_sent_cache(FIRESTORE_BATCH_LIMIT)
    batch = db.batch()
    # pending_doc_ids はバッチ確定前の重複追加を防ぐために利用
    pending_doc_ids: set[str] = set()

    sitemap_urls = fetch_sitemap_urls(sitemap_url)
    for url in sitemap_urls:
        doc_id = encode_doc_id(url)

        last_sent_exists = has_last_sent.get(doc_id, False)
        if not last_sent_exists and doc_id not in pending_doc_ids:
            doc_ref = db.collection("url_notifications").document(doc_id)
            batch.set(
                doc_ref,
                {"url": url, "last_sent": INITIAL_TIMESTAMP},
                merge=True,
            )
            pending_doc_ids.add(doc_id)
            print(f"FirestoreにURL登録: {url}")

        if len(pending_doc_ids) >= FIRESTORE_BATCH_LIMIT:
            batch = commit_pending_batch(batch, pending_doc_ids, has_last_sent)

    batch = commit_pending_batch(batch, pending_doc_ids, has_last_sent)


def build_summary_email_body_html(results: list[NotificationResult]) -> str:
    """全URL通知結果をまとめたHTMLメール本文を生成する(装飾付き)。

    Args:
        results (List[NotificationResult]): 通知結果リスト

    Returns:
        str: HTML本文
    """
    rows = "".join(
        f"<tr style='background-color:{'#eafbea' if r['status'] == 'success' else '#ffeaea'};'>"  # noqa: E501
        f"<td style='word-break:break-all;'>{r['url']}</td>"
        f"<td style='font-weight:bold;color:{'#218838' if r['status'] == 'success' else '#c82333'};'>{'成功' if r['status'] == 'success' else '失敗'}</td>"  # noqa: E501
        f"<td>{r['http_status']}</td>"
        f"<td><pre style='white-space:pre-wrap;margin:0;font-family:inherit;'>{r['message']}</pre></td>"  # noqa: E501
        f"</tr>"
        for r in results
    )
    return f"""
    <html>
      <head>
        <style>
          table.result-table {{
            border-collapse: separate;
            border-spacing: 0;
            width: 100%;
            font-family: 'Segoe UI', 'Meiryo', sans-serif;
            box-shadow: 0 2px 8px #eee;
            border-radius: 8px;
            overflow: hidden;
          }}
          .result-table th, .result-table td {{
            border: 1px solid #ccc;
            padding: 8px 12px;
            text-align: left;
          }}
          .result-table th {{
            background: #4f81bd;
            color: #fff;
            font-weight: bold;
          }}
          .result-table tr:hover {{
            background: #f1f7ff;
          }}
        </style>
      </head>
      <body>
        <h2 style='font-family:Segoe UI,Meiryo,sans-serif;'>インデックス通知バッチ結果</h2>
        <table class='result-table'>
          <tr>
            <th>URL</th><th>結果</th><th>HTTPステータス</th><th>メッセージ</th>
          </tr>
          {rows}
        </table>
      </body>
    </html>
    """  # noqa: E501


def main(request: Any) -> tuple[dict[str, Any], int]:  # noqa: ANN401, ARG001
    """Cloud Functionsのエントリポイント。
    サイトマップからURLを取得しFirestoreに登録後、未送信・古い通知をAPIに送信し更新する。

    Args:
        request (Any): HTTPリクエストオブジェクト(Cloud Functions仕様)

    Returns:
        Tuple[Dict[str, Any], int]:
            処理結果のリストまたはエラーを含む辞書と
            HTTPステータスコード
    """
    try:
        env = get_env_vars()
    except OSError as e:
        print(str(e))
        return {"error": str(e)}, 500

    print(f"認証セッションを初期化中。スコープ: {SCOPES}")
    credentials, _ = google.auth.default(scopes=SCOPES)
    authed_session = AuthorizedSession(credentials)
    print(f"認証セッションの取得に成功しました。スコープ: {SCOPES}")

    # サイトマップからURL一覧をFirestoreに登録
    print("サイトマップからURL一覧を取得し、Firestoreに登録します。")
    register_sitemap_urls_to_firestore(env["sitemap_url"])

    # Firestoreから送信待ちURLを取得
    print(f"Firestoreから送信待ちのURLを最大{BATCH_SIZE}件取得します。")
    pending_docs = get_pending_url_docs(batch_size=BATCH_SIZE)

    results: list[NotificationResult] = []
    updated_doc_refs: list[firestore.DocumentReference] = []
    for doc in pending_docs:
        url = doc.to_dict().get("url")
        if not url:
            print("URLフィールドが存在しないドキュメントをスキップしました。")
            continue
        doc_ref = doc.reference
        print(f"インデックス通知を送信中: {url}")
        success, status_code, message = send_indexing_notification(url, authed_session)
        if success:
            updated_doc_refs.append(doc_ref)
            results.append(
                {
                    "url": url,
                    "status": "success",
                    "http_status": status_code,
                    "message": "OK",
                },
            )
        else:
            results.append(
                {
                    "url": url,
                    "status": "failed",
                    "http_status": status_code,
                    "message": message,
                },
            )
        time.sleep(SLEEP_SECONDS)  # API制限緩和のため待機

    update_last_sent_timestamps(updated_doc_refs)

    # まとめてメール通知
    has_error = any(r["status"] == "failed" for r in results)
    subject_prefix = "【エラー】" if has_error else "【完了】"
    subject = f"{subject_prefix}インデックス通知バッチ結果: {len(results)}件"
    body_html = build_summary_email_body_html(results)
    try:
        msg = MIMEMultipart()
        msg["From"] = env["mail_from"]
        msg["To"] = env["mail_to"]
        msg["Subject"] = subject
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(env["mail_from"], env["mail_password"])
            server.send_message(msg)
        print(f"バッチ結果メール送信成功: {subject}")
    except Exception as e:
        print(f"バッチ結果メール送信失敗: {subject} エラー={e}")

    print("処理結果:", results)
    return {"results": results}, 200
