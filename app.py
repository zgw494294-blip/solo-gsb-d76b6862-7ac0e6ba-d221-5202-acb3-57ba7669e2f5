"""稿件匿名互评：Flask + SQLite + 原生 JavaScript。

规则要点：
- 主持人录入 3~8 人姓名与纯文本稿件，名单固定，按录入顺序循环互评（每人评下一人，末位评首位）。
- 主持链接与个人链接均为随机私密链接，无需注册。
- 参与者只能改自己的稿、读被分配的稿（不返回他人姓名/链接）、提交意见。
- 改稿立即使针对它的已交意见失效；提交绑定所读稿件版本，过期提交拒绝且不覆盖有效意见。
- 每篇当前稿都有有效意见时才可一次公布；公布后作者可见匿名意见，稿件与意见锁定。
- 公布后作者可对收到的意见提一次澄清问题，对应评审者答复一次；每条意见一问一答，
  问答只随参与状态快照返回给该作者与评审者，主持台与其他参与者均不可见。
- 个人链接泄露时，主持人可凭主持链接在主持台为指定成员换发链接（公布前后均可）：
  生成新随机链接更新主持台，旧链接立即作废——其之后发起的状态读取与一切写入均 404；
  新链接沿用同一参与者的稿件、版本、分配、意见与问答，不重建身份、不使意见失效。
- 公布前主持人可凭主持链接与姓名移除成员（不能补人、不能恢复）：留存成员按原录入顺序
  重新闭合互评环，移除后至少留 3 人。退出者的个人链接永久失效（一切读取与写入 404），
  其发出或收到的意见均不计入公布条件；仅"原来评退出者"的人改评下一位留存成员，
  其旧意见失效并换发新的匿名分配凭据，其他人的稿件、版本、意见与链接全部保留。
- 参与状态随快照返回不暴露作者身份的分配凭据；提交意见同时校验凭据与稿件版本，
  分配改变后缺少凭据或沿用旧凭据一律 409（即使两篇稿件版本号相同）；分配从未变过时
  兼容不带凭据的旧提交格式。移除、意见提交与公布在写事务内按提交顺序串行生效。
- 所有写操作在 BEGIN IMMEDIATE 事务内"先检查后变更"，拒绝即整体回滚，不留部分变更。
- 状态读取（参与页、主持台）在单个只读事务内完成：整个响应取自同一快照；
  WAL 下读取不阻塞其他连接的写入，写入也不阻塞读取。
"""

import os
import secrets
import sqlite3
from urllib.parse import urlparse

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "app.db"))
PORT = int(os.environ.get("PORT", "8000"))

MIN_PEOPLE = 3
MAX_PEOPLE = 8
MAX_NAME_LEN = 50
MAX_TEXT_LEN = 100000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_token TEXT NOT NULL UNIQUE,
    published INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    token TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    manuscript TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    assignment_nonce TEXT NOT NULL DEFAULT '',
    assignment_gen INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, position)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    reviewer_id INTEGER NOT NULL REFERENCES participants(id),
    target_id INTEGER NOT NULL REFERENCES participants(id),
    target_version INTEGER NOT NULL,
    content TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, reviewer_id)
);
-- 公布后的一问一答：每条意见至多一条澄清，问题/答案提交后不可修改
CREATE TABLE IF NOT EXISTS clarifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL UNIQUE REFERENCES reviews(id),
    question TEXT NOT NULL,
    answer TEXT,
    asked_at TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at TEXT
);
-- 换发链接时登记的历史（已作废）个人链接：仅用于换发时区分"所见链接已过期"(409)，
-- 作废链接在参与端的一切读取与写入均按不存在处理（404）
CREATE TABLE IF NOT EXISTS token_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    token TEXT NOT NULL UNIQUE
);
"""

app = Flask(__name__)


def init_db():
    """建库建表（幂等），数据落盘在 DB_PATH，重启后保留。

    旧版数据库（无 clarifications / token_history 表，或 participants 缺
    active / assignment_nonce / assignment_gen 列）启动时自动补建问答表、
    历史链接表与移除成员所需列，既有 sessions/participants/reviews 数据原样保留；
    补列时为每位既有成员回填一个分配凭据（视为其当前分配从未变过，gen=0），
    旧库因此自动支持移除成员，无需手动迁移。
    """
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(participants)").fetchall()
    }
    if "active" not in cols:
        conn.execute("ALTER TABLE participants ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if "assignment_nonce" not in cols:
        conn.execute("ALTER TABLE participants ADD COLUMN assignment_nonce TEXT NOT NULL DEFAULT ''")
    if "assignment_gen" not in cols:
        conn.execute("ALTER TABLE participants ADD COLUMN assignment_gen INTEGER NOT NULL DEFAULT 0")
    # 旧库既有成员：回填一个分配凭据，标记为"分配从未变过"（gen=0，兼容旧提交格式）
    rows = conn.execute(
        "SELECT id FROM participants WHERE assignment_nonce=''"
    ).fetchall()
    for (pid,) in rows:
        conn.execute(
            "UPDATE participants SET assignment_nonce=? WHERE id=?",
            (secrets.token_urlsafe(24), pid),
        )
    conn.commit()
    conn.close()


init_db()


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("unhandled error")
    return jsonify({"error": "服务器内部错误"}), 500


def err(message, status=400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def get_target(conn, session_id, position):
    """循环分配（按原录入顺序，在留存成员间闭合）：position 之后第一位仍留存的人；
    其后无人时回到首位仍留存者。移除成员后环自然闭合，例如 [甲,乙,丙,丁] 移除乙后，
    甲评丙、丙评丁、丁评甲。
    """
    later = conn.execute(
        "SELECT * FROM participants WHERE session_id=? AND active=1 AND position>? "
        "ORDER BY position LIMIT 1",
        (session_id, position),
    ).fetchone()
    if later is not None:
        return later
    return conn.execute(
        "SELECT * FROM participants WHERE session_id=? AND active=1 "
        "ORDER BY position LIMIT 1",
        (session_id,),
    ).fetchone()


def find_session_by_host(conn, host_token):
    return conn.execute(
        "SELECT * FROM sessions WHERE host_token=?", (host_token,)
    ).fetchone()


def find_participant(conn, token):
    # 退出（移除）成员的个人链接永久失效：连同其历史链接一律按不存在处理（404）
    return conn.execute(
        "SELECT * FROM participants WHERE token=? AND active=1", (token,)
    ).fetchone()


def find_participant_by_name(conn, session_id, name):
    # 已移除者"成员不存在"：不能凭旧姓名再次操作（不支持恢复/补人）
    return conn.execute(
        "SELECT * FROM participants WHERE session_id=? AND name=? AND active=1",
        (session_id, name),
    ).fetchone()


def extract_personal_token(link):
    """从"页面所见个人链接"取出 token：接受 /p/<token> 或完整 URL（如 http://host/p/<token>）。

    只识别形如个人链接的输入；其余返回 None，由调用方按 400 处理。
    """
    if not isinstance(link, str):
        return None
    value = link.strip()
    if value.startswith("/"):
        path = value
    else:
        parsed = urlparse(value)
        if not parsed.netloc or not parsed.scheme:
            return None
        path = parsed.path
    parts = [seg for seg in path.split("/") if seg]
    if len(parts) == 2 and parts[0] == "p":
        return parts[1]
    return None


def new_participant_token(conn):
    """生成不与任何当前链接或历史（已作废）链接冲突的随机个人链接 token。"""
    while True:
        token = secrets.token_urlsafe(24)
        exists = conn.execute(
            "SELECT 1 FROM participants WHERE token=?"
            " UNION ALL SELECT 1 FROM token_history WHERE token=? LIMIT 1",
            (token, token),
        ).fetchone()
        if exists is None:
            return token


def new_assignment_nonce(conn):
    """生成不与任何当前分配凭据冲突的随机凭据（不暴露作者身份）。"""
    while True:
        nonce = secrets.token_urlsafe(24)
        exists = conn.execute(
            "SELECT 1 FROM participants WHERE assignment_nonce=? AND active=1 LIMIT 1",
            (nonce,),
        ).fetchone()
        if exists is None:
            return nonce


def find_review_by_reviewer(conn, session_id, reviewer_id):
    return conn.execute(
        "SELECT * FROM reviews WHERE session_id=? AND reviewer_id=?",
        (session_id, reviewer_id),
    ).fetchone()


def review_is_current(review_row, target_row):
    # target 必须仍是当前被分配对象：分配改变（移除成员导致改评下一位）后旧意见失效，
    # 即便两篇稿件的版本号恰好相同
    return (
        review_row is not None
        and review_row["valid"]
        and review_row["target_id"] == target_row["id"]
        and review_row["target_version"] == target_row["version"]
    )


def find_clarification(conn, review_id):
    return conn.execute(
        "SELECT * FROM clarifications WHERE review_id=?", (review_id,)
    ).fetchone()


def clarification_view(row):
    """问答对外结构：只含问/答文本，不含任何身份信息与私密链接。"""
    if row is None:
        return None
    return {
        "question": row["question"],
        "answer": row["answer"],
        "answered": row["answer"] is not None,
    }


# ---------------- 页面 ----------------

@app.get("/")
def index_page():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/h/<token>")
def host_page(token):
    return send_from_directory(app.static_folder, "host.html")


@app.get("/p/<token>")
def participant_page(token):
    return send_from_directory(app.static_folder, "participant.html")


# ---------------- 创建会话 ----------------

@app.post("/api/sessions")
def create_session():
    data = request.get_json(silent=True) or {}
    people = data.get("people")
    if not isinstance(people, list) or not (MIN_PEOPLE <= len(people) <= MAX_PEOPLE):
        return err(f"需要 {MIN_PEOPLE}～{MAX_PEOPLE} 人")

    cleaned, names = [], []
    for p in people:
        if not isinstance(p, dict):
            return err("数据格式不正确")
        name = p.get("name")
        text = p.get("manuscript")
        if not isinstance(name, str) or not isinstance(text, str):
            return err("姓名与稿件必须是字符串")
        name = name.strip()
        if not name or len(name) > MAX_NAME_LEN:
            return err(f"姓名不能为空且不超过 {MAX_NAME_LEN} 字")
        if not text.strip() or len(text) > MAX_TEXT_LEN:
            return err(f"「{name}」的稿件不能为空且不超过 {MAX_TEXT_LEN} 字")
        cleaned.append((name, text))
        names.append(name)
    if len(set(names)) != len(names):
        return err("姓名不能重复")

    host_token = secrets.token_urlsafe(24)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("INSERT INTO sessions (host_token) VALUES (?)", (host_token,))
        session_id = cur.lastrowid
        links = []
        for i, (name, text) in enumerate(cleaned):
            token = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO participants (session_id, token, name, position, manuscript, assignment_nonce)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, token, name, i, text, secrets.token_urlsafe(24)),
            )
            links.append({"name": name, "url": f"/p/{token}"})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"host_url": f"/h/{host_token}", "people": links})


# ---------------- 主持端 ----------------

@app.get("/api/h/<host_token>")
def host_status(host_token):
    conn = get_db()
    try:
        # 只读事务：整个响应取自同一快照；WAL 下不阻塞其他连接的写入
        conn.execute("BEGIN")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        parts = conn.execute(
            "SELECT * FROM participants WHERE session_id=? AND active=1 ORDER BY position",
            (session["id"],),
        ).fetchall()
        reviewers = []
        can_publish = not session["published"]
        for p in parts:
            target = get_target(conn, session["id"], p["position"])
            review = find_review_by_reviewer(conn, session["id"], p["id"])
            submitted = review_is_current(review, target)
            if not submitted:
                can_publish = False
            reviewers.append({"name": p["name"], "submitted": submitted})
        conn.commit()
        return jsonify(
            {
                "published": bool(session["published"]),
                "can_publish": can_publish,
                "reviewers": reviewers,
                "links": [
                    {"name": p["name"], "url": f"/p/{p['token']}"} for p in parts
                ],
            }
        )
    except Exception:
        conn.rollback()
        raise


@app.post("/api/h/<host_token>/publish")
def publish(host_token):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["published"]:
            conn.rollback()
            return err("已经公布过了", 409)
        parts = conn.execute(
            "SELECT * FROM participants WHERE session_id=? AND active=1 ORDER BY position",
            (session["id"],),
        ).fetchall()
        missing = []
        for p in parts:
            review = conn.execute(
                "SELECT 1 FROM reviews r JOIN participants v ON r.reviewer_id=v.id"
                " WHERE r.target_id=? AND r.valid=1 AND r.target_version=?"
                " AND v.active=1",
                (p["id"], p["version"]),
            ).fetchone()
            if review is None:
                missing.append(p["name"])
        if missing:
            conn.rollback()
            return err("仍有稿件缺少针对当前版本的有效意见，不能公布", 409, missing=missing)
        conn.execute("UPDATE sessions SET published=1 WHERE id=?", (session["id"],))
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/h/<host_token>/rotate-link")
def rotate_link(host_token):
    """主持人为本会话某人换发个人链接（公布前后均可）。

    请求携带主持链接（路径）、姓名与主持台页面所见的个人链接；仅有效主持链接可操作
    本会话人员：主持链接无效或姓名不存在返回 404；所见链接非当前链接（已过期作废）
    返回 409；失败不改变任何数据，且任何错误响应都不返回新链接。成功时生成新的随机
    个人链接并更新主持台，旧链接登记作废——换发提交后它发起的状态读取及一切合法写入
    （改稿、意见、问题、答复）一律 404。新链接沿用同一参与者行（稿件、版本、分配、
    意见、问答均不变），其他人的链接与公布状态不变。
    """
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return err("请指定要换发链接的姓名")
    name = name.strip()
    if len(name) > MAX_NAME_LEN:
        return err(f"姓名不超过 {MAX_NAME_LEN} 字")
    seen_token = extract_personal_token(data.get("link"))
    if seen_token is None:
        return err("请提供主持台页面所见的个人链接")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        participant = find_participant_by_name(conn, session["id"], name)
        # 姓名不存在（不属于本会话）按 404，且不透露会话中的名单
        if participant is None:
            conn.rollback()
            return err("本会话中没有该成员", 404)
        # 所见链接必须与该成员当前链接一致：已作废的旧链接/他人链接/伪造链接均 409
        if participant["token"] != seen_token:
            conn.rollback()
            retired = conn.execute(
                "SELECT 1 FROM token_history WHERE token=?", (seen_token,)
            ).fetchone()
            if retired is not None:
                return err("所见的个人链接已过期，链接已被换发", 409)
            return err("所见的个人链接与该成员当前链接不一致", 409)
        old_token = participant["token"]
        new_token = new_participant_token(conn)
        # 同一事务内：旧链接登记作废 + 当前链接换为新随机链接；
        # token_history.token UNIQUE 下同一旧链接的并发换发最多一次成功
        conn.execute(
            "INSERT INTO token_history (participant_id, token) VALUES (?,?)",
            (participant["id"], old_token),
        )
        conn.execute(
            "UPDATE participants SET token=? WHERE id=?",
            (new_token, participant["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"name": name, "url": f"/p/{new_token}"})


@app.post("/api/h/<host_token>/remove")
def remove_participant(host_token):
    """主持人在公布前凭主持链接与姓名移除一名成员。

    规则（名单、分配与意见在同一写事务内一起生效，失败不留任何变更）：
    - 主持链接无效或成员（按姓名）不存在/已移除 → 404；已公布，或移除后留存成员
      不足 3 人 → 409；请求缺姓名或格式不对 → 400。
    - 退出者标记为不再留存（active=0）：其个人链接（以及换发前的历史链接）永久失效，
      之后的状态读取与一切合法写入（改稿、意见、问题、答复、换发）均 404。
    - 留存成员按原录入顺序重新闭合互评环；退出者发出或收到的意见一律不计入公布条件。
      仅"原来评退出者"的留存成员（即录入顺序中退出者前面最近的一位留存者）改评下一位
      留存成员：其旧意见失效（valid=0）并换发新的匿名分配凭据（nonce 更新、gen+1）；
      其他人的稿件、版本、意见与链接原样保留。不支持补人或恢复。
    """
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return err("请指定要移除成员的姓名")
    name = name.strip()
    if len(name) > MAX_NAME_LEN:
        return err(f"姓名不超过 {MAX_NAME_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["published"]:
            conn.rollback()
            return err("已公布，不能再移除成员", 409)
        removed = find_participant_by_name(conn, session["id"], name)
        if removed is None:
            conn.rollback()
            return err("本会话中没有该成员", 404)

        active_count = conn.execute(
            "SELECT COUNT(*) AS c FROM participants"
            " WHERE session_id=? AND active=1",
            (session["id"],),
        ).fetchone()["c"]
        if active_count <= MIN_PEOPLE:
            conn.rollback()
            return err(f"移除后不足 {MIN_PEOPLE} 人，不能移除", 409)

        # 原录入顺序中退出者前面最近的一位留存者：原来由 ta 评退出者，移除后改评下一位
        predecessor = conn.execute(
            "SELECT * FROM participants WHERE session_id=? AND active=1 AND position<?"
            " ORDER BY position DESC LIMIT 1",
            (session["id"], removed["position"]),
        ).fetchone()
        if predecessor is None:
            predecessor = conn.execute(
                "SELECT * FROM participants WHERE session_id=? AND active=1 AND position>?"
                " ORDER BY position LIMIT 1",
                (session["id"], removed["position"]),
            ).fetchone()

        # 退出者永久退出：active=0 后其 token 与全部历史链接在所有接口上按不存在处理
        conn.execute(
            "UPDATE participants SET active=0 WHERE id=?", (removed["id"],)
        )

        if predecessor is not None:
            new_target = get_target(conn, session["id"], predecessor["position"])
            # 仅 predecessor 的分配改变：旧意见失效 + 换发新的匿名分配凭据；
            # 其他人的稿件、版本、意见与链接全部保留
            conn.execute(
                "UPDATE reviews SET valid=0 WHERE reviewer_id=? AND valid=1",
                (predecessor["id"],),
            )
            conn.execute(
                "UPDATE participants SET assignment_nonce=?, assignment_gen=assignment_gen+1"
                " WHERE id=?",
                (new_assignment_nonce(conn), predecessor["id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"ok": True})


# ---------------- 参与端 ----------------

@app.get("/api/p/<token>")
def participant_state(token):
    """参与页数据：同一快照读取；不含他人姓名与任何私密链接；公布前不含收到的意见与问答。"""
    conn = get_db()
    try:
        # 只读事务：本人稿件、分配稿件、版本、意见与公布状态取自同一快照；
        # WAL 下读取暂停期间其他连接仍可完成合法写入
        conn.execute("BEGIN")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        published = bool(session["published"])
        target = get_target(conn, me["session_id"], me["position"])
        my_review = find_review_by_reviewer(conn, me["session_id"], me["id"])

        resp = {
            "name": me["name"],
            "manuscript": me["manuscript"],
            "version": me["version"],
            "published": published,
            # 匿名：不返回被评稿件作者的姓名；credential 是不暴露作者身份的分配凭据，
            # 提交意见时与稿件版本一并校验
            "assignment": {
                "manuscript": target["manuscript"],
                "version": target["version"],
                "credential": me["assignment_nonce"],
            },
            "my_review": None,
            "received": None,
            # 一问一答：公布前一律 None；公布后只返回与本人相关的那条意见的问答
            "received_clarification": None,
            "my_review_clarification": None,
        }
        if my_review is not None:
            resp["my_review"] = {
                "content": my_review["content"],
                "version": my_review["target_version"],
                "valid": review_is_current(my_review, target),
                # 失效原因之一：分配改变（原被评成员退出）改评他人，旧意见已作废
                "target_changed": my_review["target_id"] != target["id"],
            }
            # 评审者视角：我提交的那条意见收到的澄清（公布后才可见，且仅本人可见）
            if published:
                resp["my_review_clarification"] = clarification_view(
                    find_clarification(conn, my_review["id"])
                )
        if published:
            # 匿名：只返回意见内容，不返回评审人任何信息；评审者须仍留存
            row = conn.execute(
                "SELECT r.* FROM reviews r JOIN participants v ON r.reviewer_id=v.id"
                " WHERE r.target_id=? AND r.valid=1 AND r.target_version=? AND v.active=1",
                (me["id"], me["version"]),
            ).fetchone()
            if row is not None:
                resp["received"] = row["content"]
                # 作者视角：就我收到的这条意见的一问一答（仅本人与对应评审者可见）
                resp["received_clarification"] = clarification_view(
                    find_clarification(conn, row["id"])
                )
        conn.commit()
        return jsonify(resp)
    except Exception:
        conn.rollback()
        raise


@app.put("/api/p/<token>/manuscript")
def edit_manuscript(token):
    data = request.get_json(silent=True) or {}
    text = data.get("manuscript")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LEN:
        return err(f"稿件不能为空且不超过 {MAX_TEXT_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT published FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        if session["published"]:
            conn.rollback()
            return err("已公布，稿件不可再修改", 409)
        # 同一事务内：改稿 + 版本号 +1 + 使针对该稿的已交意见失效（其他意见保留）
        conn.execute(
            "UPDATE participants SET manuscript=?, version=version+1 WHERE id=?",
            (text, me["id"]),
        )
        conn.execute(
            "UPDATE reviews SET valid=0 WHERE target_id=? AND valid=1", (me["id"],)
        )
        new_version = conn.execute(
            "SELECT version FROM participants WHERE id=?", (me["id"],)
        ).fetchone()["version"]
        conn.commit()
        return jsonify({"ok": True, "version": new_version})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/p/<token>/review")
def submit_review(token):
    data = request.get_json(silent=True) or {}
    content = data.get("content")
    version = data.get("version")
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT_LEN:
        return err(f"意见不能为空且不超过 {MAX_TEXT_LEN} 字")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return err("缺少所读稿件的版本号")
    # credential 可选：分配从未变过（gen=0）时兼容旧客户端的原提交格式；
    # 分配改变后（gen>0）必须携带，且类型须为字符串
    credential = data.get("credential")
    if credential is not None and not isinstance(credential, str):
        return err("分配凭据格式不正确")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT published FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        if session["published"]:
            conn.rollback()
            return err("已公布，意见不可再提交或修改", 409)
        target = get_target(conn, me["session_id"], me["position"])
        # 先校验分配凭据：分配改变（移除成员导致改评下一位）后，缺少凭据或沿用旧凭据
        # 一律 409——即使两篇稿件的版本号恰好相同
        if me["assignment_gen"] > 0 and credential != me["assignment_nonce"]:
            conn.rollback()
            return err(
                "分配已改变，请重新阅读新分配的稿件后再提交",
                409,
                assignment_changed=True,
            )
        # 再绑定所读稿件版本：过期提交整体拒绝，不覆盖任何已有意见
        if target["version"] != version:
            conn.rollback()
            return err(
                "稿件已更新，请阅读最新版本后再提交",
                409,
                current_version=target["version"],
            )
        conn.execute(
            """
            INSERT INTO reviews (session_id, reviewer_id, target_id, target_version, content, valid, updated_at)
            VALUES (?,?,?,?,?,1,datetime('now'))
            ON CONFLICT(session_id, reviewer_id)
            DO UPDATE SET content=excluded.content,
                          target_id=excluded.target_id,
                          target_version=excluded.target_version,
                          valid=1,
                          updated_at=datetime('now')
            """,
            (me["session_id"], me["id"], target["id"], version, content),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


# ---------------- 公布后的一问一答 ----------------


@app.post("/api/p/<token>/question")
def ask_clarification(token):
    """作者就自己收到的意见提一次澄清问题：公布后开放，每条意见至多一问，提交不可修改。"""
    data = request.get_json(silent=True) or {}
    question = data.get("question")
    # 文本沿用现有意见的非空及长度限制
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_TEXT_LEN:
        return err(f"问题不能为空且不超过 {MAX_TEXT_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        # 公布前提问一律 409，且不改变任何数据
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能提问", 409)
        # 服务端按个人链接确定身份与对应意见：只能问自己收到的那条有效意见
        review = conn.execute(
            "SELECT r.* FROM reviews r JOIN participants v ON r.reviewer_id=v.id"
            " WHERE r.target_id=? AND r.valid=1 AND r.target_version=? AND v.active=1",
            (me["id"], me["version"]),
        ).fetchone()
        if review is None:
            conn.rollback()
            return err("没有可追问的意见", 409)
        # 每条意见只容纳一问；重复提交 409
        if find_clarification(conn, review["id"]) is not None:
            conn.rollback()
            return err("该意见已经提过问，不能重复提问或修改", 409)
        # review_id UNIQUE：并发提交同一问题最多一次成功，其余触发约束失败
        conn.execute(
            "INSERT INTO clarifications (review_id, question) VALUES (?,?)",
            (review["id"], question),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/p/<token>/answer")
def answer_clarification(token):
    """对应评审者就作者的澄清问题答复一次：尚无问题或已答复均 409，提交不可修改。"""
    data = request.get_json(silent=True) or {}
    answer = data.get("answer")
    # 文本沿用现有意见的非空及长度限制
    if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_TEXT_LEN:
        return err(f"答复不能为空且不超过 {MAX_TEXT_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        # 公布前答复一律 409
        if not session["published"]:
            conn.rollback()
            return err("尚未公布，不能答复", 409)
        # 服务端按个人链接确定身份：只能答复本人提交的那条意见上的问题
        review = find_review_by_reviewer(conn, me["session_id"], me["id"])
        if review is None:
            conn.rollback()
            return err("没有可答复的问题", 409)
        clarification = find_clarification(conn, review["id"])
        # 尚无问题时答复 409
        if clarification is None:
            conn.rollback()
            return err("作者尚未提问，暂不能答复", 409)
        # 每条意见只容纳一答；重复提交 409
        if clarification["answer"] is not None:
            conn.rollback()
            return err("该问题已经答复，不能重复答复或修改", 409)
        conn.execute(
            "UPDATE clarifications SET answer=?, answered_at=datetime('now') WHERE id=? AND answer IS NULL",
            (answer, clarification["id"]),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
