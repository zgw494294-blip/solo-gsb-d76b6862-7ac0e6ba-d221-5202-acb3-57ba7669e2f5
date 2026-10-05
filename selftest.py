"""可控交错自测：验证参与页状态读取的快照语义与事务/连接释放。

不依赖运行中的服务：直接以 Flask 测试客户端驱动 app，
数据库使用独立临时文件（导入 app 前通过 DB_PATH 指定），不影响正式数据。

运行：python selftest.py
"""

import logging
import os
import sqlite3
import tempfile
import threading
import time
import unittest

os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="review-selftest-"), "app.db"
)

import app as app_module  # noqa: E402  必须在设置 DB_PATH 之后导入
from flask import g  # noqa: E402


# ---------------- 测试工具 ----------------


class ConnWrapper:
    """包装 sqlite3 连接：逐语句钩子（用于暂停/注入故障），并记录关闭时的事务状态。

    hook 在语句执行完成后调用；pre_hook 在执行前调用（可截住因锁竞争而阻塞、
    尚未返回的 BEGIN IMMEDIATE）。
    """

    def __init__(self, real):
        self._real = real
        self.hook = None
        self.pre_hook = None
        self.paused = False
        self.in_transaction_at_close = None

    def execute(self, sql, parameters=()):
        if self.pre_hook is not None:
            self.pre_hook(self, sql)
        cur = self._real.execute(sql, parameters)
        if self.hook is not None:
            self.hook(self, sql)
        return cur

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        self.in_transaction_at_close = self._real.in_transaction
        return self._real.close()


class use_wrapped_db:
    """临时把 app.get_db 换成返回 ConnWrapper 的版本（连接参数与 g 缓存保持一致）。"""

    def __init__(self, hook=None, pre_hook=None):
        self.hook = hook
        self.pre_hook = pre_hook
        self.wrappers = []
        self._orig = None

    def _get_db(self):
        if "db" not in g:
            conn = sqlite3.connect(app_module.DB_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            wrapper = ConnWrapper(conn)
            wrapper.hook = self.hook
            wrapper.pre_hook = self.pre_hook
            self.wrappers.append(wrapper)
            g.db = wrapper
        return g.db

    def __enter__(self):
        self._orig = app_module.get_db
        app_module.get_db = self._get_db
        return self

    def __exit__(self, *exc):
        app_module.get_db = self._orig
        return False


def new_client():
    return app_module.app.test_client()


def create_session(client):
    resp = client.post(
        "/api/sessions",
        json={
            "people": [
                {"name": "甲", "manuscript": "文本一"},
                {"name": "乙", "manuscript": "文本二"},
                {"name": "丙", "manuscript": "文本三"},
            ]
        },
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    host_token = data["host_url"].rsplit("/", 1)[-1]
    tokens = [p["url"].rsplit("/", 1)[-1] for p in data["people"]]
    return host_token, tokens


def submit_review(client, token, content, version):
    return client.post(
        f"/api/p/{token}/review", json={"content": content, "version": version}
    )


def ask(client, token, question):
    return client.post(
        f"/api/p/{token}/question", json={"question": question}
    )


def answer(client, token, reply):
    return client.post(
        f"/api/p/{token}/answer", json={"answer": reply}
    )


def publish_with_reviews(client, host_token, tokens, reviews=("甲评乙", "乙评丙", "丙评甲")):
    """三人首轮意见全部提交并一次公布，返回各 token。"""
    for tok, content in zip(tokens, reviews):
        resp = submit_review(client, tok, content, 1)
        assert resp.status_code == 200, resp.get_json()
    resp = client.post(f"/api/h/{host_token}/publish")
    assert resp.status_code == 200, resp.get_json()


# ---------------- 用例 ----------------


class SnapshotReadTest(unittest.TestCase):
    """核心场景：读取暂停期间另一连接完成 改稿→提交新版意见→公布。"""

    def setUp(self):
        self.client = new_client()

    def test_read_snapshot_consistent_under_concurrent_writes(self):
        host_token, tokens = create_session(self.client)
        # 首轮：三人都对 v1 稿件提交了有效意见
        for tok, content in zip(tokens, ["甲评乙", "乙评丙", "丙评甲"]):
            resp = submit_review(self.client, tok, content, 1)
            self.assertEqual(resp.status_code, 200, resp.get_json())

        read_started = threading.Event()
        writes_done = threading.Event()
        armed = {"on": True}

        def pause_hook(wrapper, sql):
            s = sql.lstrip().upper()
            # 读出本人稿件（第一条 SELECT）后暂停，模拟"读出旧稿后"的交错点
            if armed["on"] and s.startswith("SELECT") and "FROM PARTICIPANTS" in s:
                armed["on"] = False
                wrapper.paused = True
                read_started.set()
                self.assertTrue(writes_done.wait(10), "写入侧未及时完成")

        result = {}

        def reader():
            result["resp"] = new_client().get(f"/api/p/{tokens[0]}")

        with use_wrapped_db(pause_hook) as db:
            t = threading.Thread(target=reader)
            t.start()
            self.assertTrue(read_started.wait(10), "读取未在第一条查询后暂停")

            # 读取暂停期间，另一连接依次完成合法写入（WAL 下不被读事务阻塞）
            resp = self.client.put(
                f"/api/p/{tokens[0]}/manuscript", json={"manuscript": "文本一（改）"}
            )
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertEqual(resp.get_json()["version"], 2)
            resp = submit_review(self.client, tokens[2], "丙评甲（新版）", 2)
            self.assertEqual(resp.status_code, 200, resp.get_json())
            resp = self.client.post(f"/api/h/{host_token}/publish")
            self.assertEqual(resp.status_code, 200, resp.get_json())

            writes_done.set()
            t.join(10)
            self.assertFalse(t.is_alive(), "读取线程未结束")

        reader_conn = next(w for w in db.wrappers if w.paused)

        resp = result["resp"]
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        # 关键断言：允许完整返回公布前状态；不得拼出
        # published=true + 旧 version + received=null 的混合快照
        self.assertEqual(
            (data["published"], data["version"], data["received"]),
            (False, 1, None),
        )
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["assignment"]["manuscript"], "文本二")
        self.assertEqual(data["assignment"]["version"], 1)
        self.assertEqual(data["my_review"]["content"], "甲评乙")
        self.assertTrue(data["my_review"]["valid"])
        # 读取事务已结束、连接已释放
        self.assertIs(reader_conn.in_transaction_at_close, False)

        # 交错结束后再读：完整落在公布后快照，received 为当前稿件的有效意见
        data = self.client.get(f"/api/p/{tokens[0]}").get_json()
        self.assertEqual((data["published"], data["version"]), (True, 2))
        self.assertEqual(data["manuscript"], "文本一（改）")
        self.assertEqual(data["received"], "丙评甲（新版）")
        self.assertTrue(data["my_review"]["valid"])

    def test_journal_mode_is_wal(self):
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_success_ends_transaction(self):
        _, tokens = create_session(self.client)
        with use_wrapped_db() as db:
            resp = self.client.get(f"/api/p/{tokens[0]}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)

    def test_invalid_token_ends_transaction(self):
        with use_wrapped_db() as db:
            resp = self.client.get("/api/p/不存在的链接")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "个人链接无效"})
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)

    def test_read_error_ends_transaction(self):
        _, tokens = create_session(self.client)

        def boom(_wrapper, sql):
            if "FROM SESSIONS" in sql.lstrip().upper():
                raise RuntimeError("注入的读取故障")

        logger = logging.getLogger(app_module.app.name)
        old_level = logger.level
        logger.setLevel(logging.CRITICAL)  # 预期的 500，静默堆栈
        try:
            with use_wrapped_db(boom) as db:
                resp = self.client.get(f"/api/p/{tokens[0]}")
        finally:
            logger.setLevel(old_level)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "服务器内部错误"})
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)


class ExistingRulesTest(unittest.TestCase):
    """回归：匿名隔离、改稿失效、过期拒绝、公布门槛与公布冻结等既有规则不变。"""

    def setUp(self):
        self.client = new_client()

    def test_rules_unchanged(self):
        host_token, tokens = create_session(self.client)
        t_jia, t_yi, t_bing = tokens

        # 匿名隔离：响应不含他人姓名与任何链接字段；公布前不含收到的意见
        resp = self.client.get(f"/api/p/{t_jia}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("乙", body)
        self.assertNotIn("丙", body)
        self.assertNotIn("token", body)
        self.assertNotIn("url", body)
        self.assertIsNone(resp.get_json()["received"])

        # 改稿失效：乙改稿后，甲针对 v1 的意见失效
        self.assertEqual(submit_review(self.client, t_jia, "甲评乙", 1).status_code, 200)
        resp = self.client.put(
            f"/api/p/{t_yi}/manuscript", json={"manuscript": "文本二（改）"}
        )
        self.assertEqual(resp.status_code, 200)

        # 过期拒绝：绑定旧版本提交被 409 拒绝，且不覆盖已有意见
        resp = submit_review(self.client, t_jia, "甲评乙（过期）", 1)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["current_version"], 2)
        data = self.client.get(f"/api/p/{t_jia}").get_json()
        self.assertEqual(data["my_review"]["content"], "甲评乙")
        self.assertFalse(data["my_review"]["valid"])
        # 绑定新版本后提交成功
        self.assertEqual(
            submit_review(self.client, t_jia, "甲评乙（新版）", 2).status_code, 200
        )

        # 公布门槛：仍有稿件缺有效意见时 409
        resp = self.client.post(f"/api/h/{host_token}/publish")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(set(resp.get_json()["missing"]), {"甲", "丙"})
        self.assertEqual(submit_review(self.client, t_yi, "乙评丙", 1).status_code, 200)
        self.assertEqual(submit_review(self.client, t_bing, "丙评甲", 1).status_code, 200)
        resp = self.client.post(f"/api/h/{host_token}/publish")
        self.assertEqual(resp.status_code, 200)

        # 公布冻结：重复公布、改稿、提交意见均 409
        self.assertEqual(
            self.client.post(f"/api/h/{host_token}/publish").status_code, 409
        )
        self.assertEqual(
            self.client.put(
                f"/api/p/{t_jia}/manuscript", json={"manuscript": "冻结后改稿"}
            ).status_code,
            409,
        )
        self.assertEqual(submit_review(self.client, t_jia, "冻结后提交", 2).status_code, 409)

        # 公布后：作者看到针对当前版本的有效意见
        data = self.client.get(f"/api/p/{t_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["received"], "丙评甲")

        # 无效链接 404
        self.assertEqual(self.client.get("/api/p/无效").status_code, 404)
        self.assertEqual(self.client.get("/api/h/无效").status_code, 404)


class ClarificationTest(unittest.TestCase):
    """公布后一问一答：409 规则、身份隔离、文本限制、快照读取。"""

    def setUp(self):
        self.client = new_client()
        self.host_token, self.tokens = create_session(self.client)
        self.t_jia, self.t_yi, self.t_bing = self.tokens
        publish_with_reviews(self.client, self.host_token, self.tokens)

    def test_409_rules_and_full_flow(self):
        # 公布前不能提问/答复（在另一个新会话上验证，本会话已公布）
        c2 = new_client()
        _, t2 = create_session(c2)
        self.assertEqual(ask(c2, t2[0], "公布前提问").status_code, 409)
        self.assertEqual(answer(c2, t2[0], "公布前答复").status_code, 409)

        # 甲收到的是丙的意见；尚无问题时答复 409
        self.assertEqual(answer(self.client, self.t_bing, "抢答").status_code, 409)

        # 甲提问成功
        resp = ask(self.client, self.t_jia, "请澄清Q1")
        self.assertEqual(resp.status_code, 200, resp.get_json())

        # 重复提问 409，问题不变
        resp = ask(self.client, self.t_jia, "再问一次")
        self.assertEqual(resp.status_code, 409)
        data = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertEqual(data["received_clarification"]["question"], "请澄清Q1")
        self.assertFalse(data["received_clarification"]["answered"])
        self.assertIsNone(data["received_clarification"]["answer"])

        # 身份隔离：乙不是该意见的评审者（评审者是丙），不能答复甲的问题；
        # 乙就"自己收到的"意见另开一问是合法的，不影响甲这条一问一答
        self.assertEqual(ask(self.client, self.t_yi, "乙对自己收到的意见提问").status_code, 200)
        self.assertEqual(answer(self.client, self.t_yi, "乙来抢答甲的问题").status_code, 409)

        # 评审者丙答复成功
        resp = answer(self.client, self.t_bing, "答复A1")
        self.assertEqual(resp.status_code, 200, resp.get_json())

        # 重复答复 409，答复不变
        resp = answer(self.client, self.t_bing, "再答一次")
        self.assertEqual(resp.status_code, 409)

        # 双方各自参与页读到同一条问答
        da = self.client.get(f"/api/p/{self.t_jia}").get_json()
        db = self.client.get(f"/api/p/{self.t_bing}").get_json()
        self.assertEqual(
            da["received_clarification"],
            {"question": "请澄清Q1", "answer": "答复A1", "answered": True},
        )
        self.assertEqual(
            db["my_review_clarification"],
            {"question": "请澄清Q1", "answer": "答复A1", "answered": True},
        )

        # 回答后作者侧仍不能再次提问
        self.assertEqual(ask(self.client, self.t_jia, "第二个问题").status_code, 409)

    def test_visibility_isolation(self):
        # 提问前：所有参与页两个问答字段为 None；主持台完全不含问答
        for tok in self.tokens:
            data = self.client.get(f"/api/p/{tok}").get_json()
            self.assertIsNone(data["received_clarification"])
            self.assertIsNone(data["my_review_clarification"])
        host_body = self.client.get(f"/api/h/{self.host_token}").get_data(as_text=True)
        self.assertNotIn("clarification", host_body)
        self.assertNotIn("question", host_body)
        self.assertNotIn("answer", host_body)

        ask(self.client, self.t_jia, "甲的问题正文XYZ")
        answer(self.client, self.t_bing, "丙的答复正文UVW")

        # 甲（作者）：收到问答；自己没提问过别人（甲评乙，乙未问），评审视角为 None
        da = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertIsNone(da["my_review_clarification"])
        self.assertEqual(da["received_clarification"]["question"], "甲的问题正文XYZ")

        # 丙（评审者）：评审视角有问答；丙收到的是乙的意见，乙未提问，作者视角为 None
        db = self.client.get(f"/api/p/{self.t_bing}").get_json()
        self.assertIsNone(db["received_clarification"])
        self.assertEqual(db["my_review_clarification"]["answer"], "丙的答复正文UVW")

        # 乙是无关第三方：两侧都为 None，且响应体不含问答正文
        body = self.client.get(f"/api/p/{self.t_yi}").get_data(as_text=True)
        self.assertNotIn("正文XYZ", body)
        self.assertNotIn("正文UVW", body)

        # 主持状态始终不含问答内容
        host_body = self.client.get(f"/api/h/{self.host_token}").get_data(as_text=True)
        self.assertNotIn("正文XYZ", host_body)
        self.assertNotIn("正文UVW", host_body)

        # 匿名：响应不含对方姓名与任何私密链接
        self.assertNotIn(self.t_bing, self.client.get(f"/api/p/{self.t_jia}").get_data(as_text=True))
        self.assertNotIn("甲", self.client.get(f"/api/p/{self.t_bing}").get_data(as_text=True))

    def test_text_limits(self):
        # 空白与超长均 400（沿用意见的非空及长度限制）
        self.assertEqual(ask(self.client, self.t_jia, "   ").status_code, 400)
        self.assertEqual(ask(self.client, self.t_jia, "x" * 100001).status_code, 400)
        ask(self.client, self.t_jia, "Q")
        self.assertEqual(answer(self.client, self.t_bing, "").status_code, 400)
        self.assertEqual(answer(self.client, self.t_bing, "y" * 100001).status_code, 400)
        # 被拒绝后正常长度仍可走通（失败不改变数据）
        self.assertEqual(answer(self.client, self.t_bing, "A").status_code, 200)

    def test_failed_attempts_change_nothing(self):
        # 各种针对甲这条意见的非法尝试后：该意见无问答行产生，原稿与原意见冻结不变
        answer(self.client, self.t_bing, "提前答复")        # 尚无问题
        answer(self.client, self.t_yi, "冒名答复")          # 乙不是该意见评审者
        ask(self.client, self.t_jia, " ")                  # 空白文本
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM clarifications c"
                " JOIN reviews r ON c.review_id = r.id"
                " JOIN participants p ON r.target_id = p.id WHERE p.token=?",
                (self.t_jia,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)
        data = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["received"], "丙评甲")
        self.assertEqual(data["version"], 1)

    def test_concurrent_question_only_one_wins(self):
        results = []

        def worker():
            c = new_client()
            results.append(ask(c, self.t_jia, "并发问题").status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(results), [200, 409, 409, 409, 409])
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM clarifications c"
                " JOIN reviews r ON c.review_id = r.id"
                " JOIN participants p ON r.target_id = p.id WHERE p.token=?",
                (self.t_jia,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_concurrent_answer_only_one_wins(self):
        self.assertEqual(ask(self.client, self.t_jia, "问题").status_code, 200)
        results = []

        def worker():
            c = new_client()
            results.append(answer(c, self.t_bing, "并发答复").status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(results), [200, 409, 409, 409, 409])
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            row = conn.execute(
                "SELECT c.question, c.answer FROM clarifications c"
                " JOIN reviews r ON c.review_id = r.id"
                " JOIN participants p ON r.target_id = p.id WHERE p.token=?",
                (self.t_jia,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("问题", "并发答复"))

    def test_invalid_token_404(self):
        self.assertEqual(ask(new_client(), "无效链接", "Q").status_code, 404)
        self.assertEqual(answer(new_client(), "无效链接", "A").status_code, 404)

    def test_qa_in_same_snapshot(self):
        ask(self.client, self.t_jia, "快照问题")
        answer(self.client, self.t_bing, "快照答复")
        # 问答随参与状态在同一响应中读取，且与公布状态、稿件版本一致
        data = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["received"], "丙评甲")
        self.assertEqual(data["received_clarification"]["question"], "快照问题")
        self.assertEqual(data["received_clarification"]["answer"], "快照答复")
        # 原稿与原意见仍冻结：公布后写操作继续 409
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.t_jia}/manuscript", json={"manuscript": "x"}
            ).status_code,
            409,
        )
        self.assertEqual(submit_review(self.client, self.t_jia, "x", 1).status_code, 409)


class RotateLinkTest(unittest.TestCase):
    """个人链接换发：404/409/400 规则、旧链接全接口失效、数据沿用、并发与提交顺序、持久化。"""

    def setUp(self):
        self.client = new_client()
        self.host_token, self.tokens = create_session(self.client)
        self.t_jia, self.t_yi, self.t_bing = self.tokens

    def rotate(self, client, name, link, host_token=None):
        return client.post(
            f"/api/h/{host_token or self.host_token}/rotate-link",
            json={"name": name, "link": link},
        )

    def host_links(self, client=None):
        resp = (client or self.client).get(f"/api/h/{self.host_token}")
        assert resp.status_code == 200, resp.get_json()
        return {l["name"]: l["url"].rsplit("/", 1)[-1] for l in resp.get_json()["links"]}

    def test_rotate_before_publish_keeps_everything_and_old_link_dies(self):
        # 公布前：先有一轮意见，乙再改稿，甲改投新版意见
        self.assertEqual(submit_review(self.client, self.t_jia, "甲评乙", 1).status_code, 200)
        self.assertEqual(
            self.client.put(
                f"/api/p/{self.t_yi}/manuscript", json={"manuscript": "文本二（改）"}
            ).status_code,
            200,
        )
        self.assertEqual(
            submit_review(self.client, self.t_jia, "甲评乙（新版）", 2).status_code, 200
        )
        self.assertEqual(submit_review(self.client, self.t_yi, "乙评丙", 1).status_code, 200)
        self.assertEqual(submit_review(self.client, self.t_bing, "丙评甲", 1).status_code, 200)

        old_jia = self.t_jia
        resp = self.rotate(self.client, "甲", f"/p/{old_jia}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertNotEqual(new_jia, old_jia)
        # 成功响应只含姓名与新链接
        self.assertEqual(set(resp.get_json().keys()), {"name", "url"})

        # 主持台立即展示新链接；其他人链接不变
        links = self.host_links()
        self.assertEqual(links["甲"], new_jia)
        self.assertEqual(links["乙"], self.t_yi)
        self.assertEqual(links["丙"], self.t_bing)

        # 旧链接：状态读取与各类写入全部 404，响应不透露新链接
        old_resp = self.client.get(f"/api/p/{old_jia}")
        self.assertEqual(old_resp.status_code, 404)
        self.assertNotIn(new_jia, old_resp.get_data(as_text=True))
        self.assertEqual(
            self.client.put(
                f"/api/p/{old_jia}/manuscript", json={"manuscript": "偷改"}
            ).status_code,
            404,
        )
        self.assertEqual(
            submit_review(self.client, old_jia, "旧链接意见", 2).status_code, 404
        )
        # 公布前提问/答复本是 409，但链接失效时一律 404
        self.assertEqual(ask(self.client, old_jia, "旧链接提问").status_code, 404)
        self.assertEqual(answer(self.client, old_jia, "旧链接答复").status_code, 404)

        # 新链接沿用同一参与者：姓名、稿件版本、分配、意见均原样
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertEqual(data["name"], "甲")
        self.assertFalse(data["published"])
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["assignment"]["manuscript"], "文本二（改）")
        self.assertEqual(data["assignment"]["version"], 2)
        self.assertEqual(data["my_review"]["content"], "甲评乙（新版）")
        self.assertTrue(data["my_review"]["valid"])

        # 参与者行未重建：id 不变，意见未失效；新链接可正常继续业务并完成公布
        self.assertEqual(
            self.client.put(
                f"/api/p/{new_jia}/manuscript", json={"manuscript": "文本一（甲再改）"}
            ).status_code,
            200,
        )
        # 丙针对甲的意见因甲改稿失效，丙用新链接读到的是乙稿……此处由丙改投甲 v2
        self.assertEqual(
            submit_review(self.client, self.t_bing, "丙评甲（新版）", 2).status_code, 200
        )
        self.assertEqual(
            self.client.post(f"/api/h/{self.host_token}/publish").status_code, 200
        )
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["manuscript"], "文本一（甲再改）")
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["received"], "丙评甲（新版）")
        # 旧链接在公布后依旧 404
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)

    def test_rotate_after_publish_keeps_reviews_and_qa(self):
        publish_with_reviews(self.client, self.host_token, self.tokens)
        ask(self.client, self.t_jia, "甲的澄清")
        answer(self.client, self.t_bing, "丙的答复")

        old_jia = self.t_jia
        resp = self.rotate(self.client, "甲", f"http://localhost:8000/p/{old_jia}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]

        # 新链接：公布状态、收到的匿名意见、一问一答全部沿用
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["received"], "丙评甲")
        self.assertEqual(
            data["received_clarification"],
            {"question": "甲的澄清", "answer": "丙的答复", "answered": True},
        )
        # 评审者丙的视角问答不随甲换链接而变化
        data = self.client.get(f"/api/p/{self.t_bing}").get_json()
        self.assertEqual(data["my_review_clarification"]["question"], "甲的澄清")

        # 公布后冻结规则保留：新链接改稿/提意见仍 409；一问一答已提交不可再改
        self.assertEqual(
            self.client.put(
                f"/api/p/{new_jia}/manuscript", json={"manuscript": "x"}
            ).status_code,
            409,
        )
        self.assertEqual(submit_review(self.client, new_jia, "x", 1).status_code, 409)
        self.assertEqual(ask(self.client, new_jia, "再问").status_code, 409)
        # 旧链接仍是 404（而不是公布冻结的 409）
        self.assertEqual(
            self.client.put(
                f"/api/p/{old_jia}/manuscript", json={"manuscript": "x"}
            ).status_code,
            404,
        )

    def test_404_invalid_host_or_unknown_name_changes_nothing(self):
        # 无效主持链接：404
        resp = self.rotate(self.client, "甲", f"/p/{self.t_jia}", host_token="假主持")
        self.assertEqual(resp.status_code, 404)
        # 姓名不属于本会话：404，且不透露名单
        resp = self.rotate(self.client, "不存在的人", f"/p/{self.t_jia}")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("url", resp.get_json())
        # 失败不改数据：链接仍是旧链接
        self.assertEqual(self.host_links()["甲"], self.t_jia)
        data = self.client.get(f"/api/p/{self.t_jia}").get_json()
        self.assertEqual(data["name"], "甲")

    def test_409_expired_or_mismatched_seen_link(self):
        old_jia = self.t_jia
        resp = self.rotate(self.client, "甲", f"/p/{old_jia}")
        self.assertEqual(resp.status_code, 200)
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]

        # 再拿已过期的旧所见链接换发：409，不返回新链接，新链接不变
        resp = self.rotate(self.client, "甲", f"/p/{old_jia}")
        self.assertEqual(resp.status_code, 409)
        self.assertNotIn("url", resp.get_json())
        self.assertNotIn(new_jia, resp.get_data(as_text=True))
        self.assertEqual(self.host_links()["甲"], new_jia)

        # 伪造链接 / 他人当前链接：均 409
        self.assertEqual(self.rotate(self.client, "甲", "/p/根本不存在").status_code, 409)
        self.assertEqual(
            self.rotate(self.client, "甲", f"/p/{self.t_yi}").status_code, 409
        )
        self.assertEqual(self.host_links()["甲"], new_jia)

        # 用当前链接可再次换发成功（连环换发）；上一次的新链接也进入作废历史
        resp = self.rotate(self.client, "甲", f"/p/{new_jia}")
        self.assertEqual(resp.status_code, 200)
        newest = resp.get_json()["url"].rsplit("/", 1)[-1]
        self.assertEqual(self.host_links()["甲"], newest)
        self.assertEqual(self.client.get(f"/api/p/{new_jia}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/p/{newest}").status_code, 200)

    def test_400_malformed_request(self):
        for payload in [
            {},
            {"name": "甲"},
            {"link": f"/p/{self.t_jia}"},
            {"name": "  ", "link": f"/p/{self.t_jia}"},
            {"name": "甲", "link": "不是链接"},
            {"name": "甲", "link": "/h/xxx"},
            {"name": "甲", "link": "http://localhost:8000/api/p/xxx"},
        ]:
            resp = self.client.post(
                f"/api/h/{self.host_token}/rotate-link", json=payload
            )
            self.assertEqual(resp.status_code, 400, payload)
        # 全部失败后链接不变
        self.assertEqual(self.host_links()["甲"], self.t_jia)

    def test_concurrent_rotate_same_old_link_only_one_wins(self):
        old_jia = self.t_jia
        results = []

        def worker():
            c = new_client()
            results.append(self.rotate(c, "甲", f"/p/{old_jia}").status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(results), [200, 409, 409, 409, 409])
        # 只有一个当前链接，旧链接失效
        cur = self.host_links()["甲"]
        self.assertNotEqual(cur, old_jia)
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/p/{cur}").status_code, 200)
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            history = conn.execute(
                "SELECT COUNT(*) FROM token_history WHERE token=?", (old_jia,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(history, 1)

    def test_rotate_first_old_credential_write_rejected(self):
        """换发先拿到写锁：旧凭据写入在换发提交后才继续，必须整体 404，数据不动。"""
        old_jia = self.t_jia
        rotate_entered = threading.Event()
        release_rotate = threading.Event()
        writer_begin = threading.Event()
        tls = threading.local()
        armed = {"rotate": True}

        def hook(_wrapper, sql):
            s = sql.lstrip().upper()
            if armed["rotate"] and "UPDATE PARTICIPANTS SET TOKEN" in s:
                armed["rotate"] = False
                rotate_entered.set()
                self.assertTrue(release_rotate.wait(10), "换发未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "writer", False) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                # 旧凭据写入即将在写锁上排队；此时换发仍未提交
                self.assertFalse(release_rotate.is_set())
                writer_begin.set()

        result = {}

        def rotator():
            tls.rotator = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["rotate"] = self.rotate(new_client(), "甲", f"/p/{old_jia}").status_code

        t = threading.Thread(target=rotator)
        t.start()
        self.assertTrue(rotate_entered.wait(10), "换发未进入更新阶段")

        def writer():
            tls.writer = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["write"] = new_client().put(
                    f"/api/p/{old_jia}/manuscript", json={"manuscript": "偷改内容"}
                ).status_code

        w = threading.Thread(target=writer)
        w.start()
        # 旧凭据写入的 BEGIN IMMEDIATE 已发起（即在写锁上排队）后，再提交换发
        self.assertTrue(writer_begin.wait(10), "旧凭据写入未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("write", result, "旧凭据写入不应在换发提交前完成")
        release_rotate.set()
        t.join(10)
        w.join(10)
        self.assertFalse(t.is_alive() or w.is_alive())
        self.assertEqual(result["rotate"], 200)
        self.assertEqual(result["write"], 404)
        # 新链接读到的稿件未被偷改
        new_jia = self.host_links()["甲"]
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["version"], 1)

    def test_old_credential_write_first_is_kept(self):
        """旧凭据写入先拿到写锁并提交：换发等待后照常成功，新链接看到已提交的新版本。"""
        old_jia = self.t_jia
        writer_past_check = threading.Event()
        release_writer = threading.Event()
        rotate_begin = threading.Event()
        tls = threading.local()
        armed = {"writer": True}

        def hook(_wrapper, sql):
            if armed["writer"] and sql.lstrip().upper().startswith("UPDATE PARTICIPANTS"):
                armed["writer"] = False
                writer_past_check.set()
                self.assertTrue(release_writer.wait(10), "写入未被释放")

        def pre_hook(_wrapper, sql):
            if getattr(tls, "rotator", False) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                # 换发已在写锁上排队，而旧凭据写入尚未提交
                self.assertFalse(release_writer.is_set())
                rotate_begin.set()

        result = {}

        def writer():
            tls.writer = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["write"] = new_client().put(
                    f"/api/p/{old_jia}/manuscript", json={"manuscript": "甲的新版本稿"}
                ).status_code

        w = threading.Thread(target=writer)
        w.start()
        self.assertTrue(writer_past_check.wait(10), "旧凭据写入未进入更新阶段")

        def rotator():
            tls.rotator = True
            with use_wrapped_db(hook, pre_hook=pre_hook):
                result["rotate"] = self.rotate(
                    new_client(), "甲", f"/p/{old_jia}"
                ).status_code

        t = threading.Thread(target=rotator)
        t.start()
        self.assertTrue(rotate_begin.wait(10), "换发未发起写事务")
        time.sleep(0.2)
        self.assertNotIn("rotate", result, "换发不应在旧凭据写入提交前完成")
        release_writer.set()
        w.join(10)
        t.join(10)
        self.assertFalse(w.is_alive() or t.is_alive())
        self.assertEqual(result["write"], 200)
        self.assertEqual(result["rotate"], 200)
        # 已提交写入保留；新链接直接看到新版本
        new_jia = self.host_links()["甲"]
        data = self.client.get(f"/api/p/{new_jia}").get_json()
        self.assertEqual(data["manuscript"], "甲的新版本稿")
        self.assertEqual(data["version"], 2)
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)

    def test_persistence_across_restart(self):
        old_jia = self.t_jia
        resp = self.rotate(self.client, "甲", f"/p/{old_jia}")
        new_jia = resp.get_json()["url"].rsplit("/", 1)[-1]
        # 模拟重启：幂等建表，数据在同一 SQLite 文件中
        app_module.init_db()
        self.assertEqual(self.client.get(f"/api/p/{new_jia}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/p/{new_jia}").get_json()["name"], "甲")
        self.assertEqual(self.client.get(f"/api/p/{old_jia}").status_code, 404)
        # 重启后旧链接仍不能拿去换发（409），主持链接照常有效
        self.assertEqual(self.rotate(self.client, "甲", f"/p/{old_jia}").status_code, 409)
        self.assertEqual(self.host_links()["甲"], new_jia)


class OldSchemaMigrationTest(unittest.TestCase):
    """旧库启动时自动支持问答与换发存储：补建 clarifications/token_history，旧数据保留。"""
    """旧库启动时自动支持问答存储：补建 clarifications，旧数据保留，重启后内容保留。"""

    OLD_SCHEMA = """
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host_token TEXT NOT NULL UNIQUE,
        published INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        token TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        position INTEGER NOT NULL,
        manuscript TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, position)
    );
    CREATE TABLE reviews (
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
    """

    def test_old_db_auto_upgraded_and_persists(self):
        tmp_dir = tempfile.mkdtemp(prefix="review-olddb-")
        old_path = os.path.join(tmp_dir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute("INSERT INTO sessions (id, host_token, published) VALUES (1,'ht',1)")
        conn.execute(
            "INSERT INTO participants (id, session_id, token, name, position, manuscript, version)"
            " VALUES (1,1,'tk','旧库作者',0,'旧稿',1)"
        )
        conn.execute(
            "INSERT INTO reviews (id, session_id, reviewer_id, target_id, target_version, content, valid)"
            " VALUES (1,1,1,1,1,'旧意见',1)"
        )
        conn.commit()
        conn.close()

        original = app_module.DB_PATH
        app_module.DB_PATH = old_path
        try:
            # 模拟新版本启动：旧库自动补建问答表
            app_module.init_db()

            c = sqlite3.connect(old_path)
            try:
                tables = {
                    r[0]
                    for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            finally:
                c.close()
            self.assertIn("clarifications", tables)
            self.assertIn("token_history", tables)

            # 旧数据原样保留，且新表可直接读写
            client = new_client()
            data = client.get("/api/p/tk").get_json()
            self.assertEqual(data["name"], "旧库作者")
            self.assertEqual(data["received"], "旧意见")
            self.assertEqual(ask(client, "tk", "旧库追问").status_code, 200)

            # 旧库上同样可以换发链接：旧链接作废，新链接沿用稿件与意见、问答
            resp = client.post(
                "/api/h/ht/rotate-link", json={"name": "旧库作者", "link": "/p/tk"}
            )
            self.assertEqual(resp.status_code, 200, resp.get_json())
            new_tk = resp.get_json()["url"].rsplit("/", 1)[-1]
            self.assertNotEqual(new_tk, "tk")
            self.assertEqual(client.get("/api/p/tk").status_code, 404)
            data = client.get(f"/api/p/{new_tk}").get_json()
            self.assertEqual(data["name"], "旧库作者")
            self.assertEqual(data["received"], "旧意见")
            self.assertEqual(data["received_clarification"]["question"], "旧库追问")

            # 模拟重启：再次 init_db 幂等，问答内容保留，新链接仍有效、旧链接仍失效
            app_module.init_db()
            data = client.get(f"/api/p/{new_tk}").get_json()
            self.assertEqual(data["received_clarification"]["question"], "旧库追问")
            self.assertEqual(client.get("/api/p/tk").status_code, 404)
        finally:
            app_module.DB_PATH = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
