import base64
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import firestore
from googleapiclient.discovery import build

# 定数定義
SCOPES: List[str] = ["https://www.googleapis.com/auth/indexing"]
ENDPOINT: str = "https://indexing.googleapis.com/v3/urlNotifications:publish"
BATCH_SIZE: int = 5
SLEEP_SECONDS: int = 10  # API制限緩和のための待機時間（秒）
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

db = firestore.Client()


class EnvVars(TypedDict):
    blogger_api_key: str
    blog_id: str
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
        "blogger_api_key": os.environ.get("BLOGGER_INDEX_REGIST_API_KEY"),
        "blog_id": os.environ.get("BLOG_ID"),
        "mail_from": os.environ.get("MAIL_FROM"),
        "mail_password": os.environ.get("MAIL_PASSWORD"),
        "mail_to": os.environ.get("MAIL_TO"),
    }
    for key, value in env.items():
        if not value:
            raise EnvironmentError(f"環境変数 {key.upper()} が設定されていません。")
    return env  # type: ignore


def encode_doc_id(url: str) -> str:
    """URLをBase64 URLセーフエンコードしてFirestoreのドキュメントIDに変換する。

    Args:
        url (str): エンコード対象のURL

    Returns:
        str: エンコード後の文字列
    """
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8")


def get_pending_url_docs(batch_size: int) -> List[firestore.DocumentSnapshot]:
    """Firestoreから送信が古い、もしくは未送信のURL通知ドキュメントを指定数取得する。

    Args:
        batch_size (int): 取得するドキュメント数の上限

    Returns:
        List[firestore.DocumentSnapshot]: 取得したドキュメントリスト
    """
    docs = (
        db.collection("url_notifications")
        .order_by("last_sent")
        .limit(batch_size)
        .stream()
    )
    return list(docs)


def update_last_sent_timestamp(doc_ref: firestore.DocumentReference) -> None:
    """Firestoreのドキュメントのlast_sentフィールドをサーバータイムスタンプで更新する。

    Args:
        doc_ref (firestore.DocumentReference): 更新対象のドキュメント参照
    """
    doc_ref.update({"last_sent": firestore.SERVER_TIMESTAMP})


def send_indexing_notification(
    url: str, authed_session: AuthorizedSession
) -> Tuple[bool, int, str]:
    """インデックス登録APIにURL更新通知を送信する。

    Args:
        url (str): 通知対象のURL
        authed_session (AuthorizedSession): 認証済みHTTPセッション

    Returns:
        Tuple[bool, int, str]: (送信成功か, HTTPステータスコード, レスポンステキスト)
    """
    payload = {"url": url, "type": "URL_UPDATED"}
    response = authed_session.post(ENDPOINT, json=payload)
    success = response.status_code == 200
    if success:
        print(f"通知送信成功: URL={url} ステータスコード={response.status_code}")
    else:
        print(
            f"通知送信失敗: URL={url} ステータスコード={response.status_code} レスポンス={response.text}"
        )
    return success, response.status_code, response.text


def register_blog_urls_to_firestore(blog_id: str, api_key: str) -> None:
    """Blogger APIからブログ投稿URL一覧を取得し、Firestoreに登録する。

    Args:
        blog_id (str): ブログID
        api_key (str): APIキー
    """
    service = build("blogger", "v3", developerKey=api_key)
    page_token: Optional[str] = None

    while True:
        posts_response: Dict[str, Any] = (
            service.posts().list(blogId=blog_id, pageToken=page_token).execute()
        )
        for post in posts_response.get("items", []):
            url: str = post["url"]
            doc_id = encode_doc_id(url)
            doc_ref = db.collection("url_notifications").document(doc_id)

            # ドキュメントの存在チェック
            doc = doc_ref.get()
            if doc.exists:
                # 既存ドキュメントにlast_sentがなければ初期化
                data = doc.to_dict()
                if "last_sent" not in data:
                    doc_ref.update({"last_sent": firestore.SERVER_TIMESTAMP})
                # URLは念のため更新（merge=Trueにより上書きは避ける）
                doc_ref.set({"url": url}, merge=True)
            else:
                # 新規登録時はlast_sentも初期化して登録
                doc_ref.set({"url": url, "last_sent": firestore.SERVER_TIMESTAMP})

            print(f"FirestoreにURL登録: {url}")
        page_token = posts_response.get("nextPageToken")
        if not page_token:
            break


def build_summary_email_body_html(results: List[NotificationResult]) -> str:
    """全URL通知結果をまとめたHTMLメール本文を生成する（装飾付き）。

    Args:
        results (List[NotificationResult]): 通知結果リスト

    Returns:
        str: HTML本文
    """
    rows = "".join(
        f"<tr style='background-color:{'#eafbea' if r['status']=='success' else '#ffeaea'};'>"
        f"<td style='word-break:break-all;'>{r['url']}</td>"
        f"<td style='font-weight:bold;color:{'#218838' if r['status']=='success' else '#c82333'};'>{'成功' if r['status']=='success' else '失敗'}</td>"
        f"<td>{r['http_status']}</td>"
        f"<td><pre style='white-space:pre-wrap;margin:0;font-family:inherit;'>{r['message']}</pre></td>"
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
    """


def main(request: Any) -> Dict[str, List[NotificationResult]]:
    """Cloud Functionsのエントリポイント。
    Blogger APIからURLを取得しFirestoreに登録後、未送信・古い通知をAPIに送信し更新する。

    Args:
        request (Any): HTTPリクエストオブジェクト（Cloud Functions仕様）

    Returns:
        Dict[str, List[NotificationResult]]: 処理結果のリストを含む辞書
    """
    try:
        env = get_env_vars()
    except EnvironmentError as e:
        print(str(e))
        return {"error": str(e)}, 500

    print(f"認証セッションを初期化中。スコープ: {SCOPES}")
    credentials, _ = google.auth.default(scopes=SCOPES)
    authed_session = AuthorizedSession(credentials)
    print(f"認証セッションの取得に成功しました。スコープ: {SCOPES}")

    # Blogger APIからURL一覧をFirestoreに登録
    print("Blogger APIからURL一覧を取得し、Firestoreに登録します。")
    register_blog_urls_to_firestore(
        blog_id=env["blog_id"], api_key=env["blogger_api_key"]
    )

    # Firestoreから送信待ちURLを取得
    print(f"Firestoreから送信待ちのURLを最大{BATCH_SIZE}件取得します。")
    pending_docs = get_pending_url_docs(batch_size=BATCH_SIZE)

    results: List[NotificationResult] = []
    for doc in pending_docs:
        url = doc.to_dict().get("url")
        if not url:
            print("URLフィールドが存在しないドキュメントをスキップしました。")
            continue
        doc_ref = doc.reference
        print(f"インデックス通知を送信中: {url}")
        success, status_code, message = send_indexing_notification(url, authed_session)
        if success:
            update_last_sent_timestamp(doc_ref)
            results.append(
                {
                    "url": url,
                    "status": "success",
                    "http_status": status_code,
                    "message": "OK",
                }
            )
        else:
            results.append(
                {
                    "url": url,
                    "status": "failed",
                    "http_status": status_code,
                    "message": message,
                }
            )
        time.sleep(SLEEP_SECONDS)  # API制限緩和のため待機

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
    return {"results": results}
