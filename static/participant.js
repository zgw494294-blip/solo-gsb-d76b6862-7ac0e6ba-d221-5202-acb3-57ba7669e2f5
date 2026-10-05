"use strict";

const token = location.pathname.split("/").filter(Boolean)[1];

const els = {
  name: document.getElementById("my-name"),
  version: document.getElementById("my-version"),
  manuscript: document.getElementById("my-manuscript"),
  saveMs: document.getElementById("save-manuscript"),
  msMsg: document.getElementById("ms-msg"),
  assignment: document.getElementById("assignment"),
  assignmentVersion: document.getElementById("assignment-version"),
  assignmentNotice: document.getElementById("assignment-notice"),
  reassignNotice: document.getElementById("reassign-notice"),
  confirmReassign: document.getElementById("confirm-reassign"),
  reviewStatus: document.getElementById("review-status"),
  review: document.getElementById("my-review"),
  submitReview: document.getElementById("submit-review"),
  reviewMsg: document.getElementById("review-msg"),
  publishedBanner: document.getElementById("published-banner"),
  receivedSection: document.getElementById("received-section"),
  received: document.getElementById("received"),
  questionBox: document.getElementById("question-box"),
  questionForm: document.getElementById("question-form"),
  questionInput: document.getElementById("question-input"),
  submitQuestion: document.getElementById("submit-question"),
  questionMsg: document.getElementById("question-msg"),
  questionDone: document.getElementById("question-done"),
  questionText: document.getElementById("question-text"),
  answerView: document.getElementById("answer-view"),
  answerBox: document.getElementById("answer-box"),
  answerQuestion: document.getElementById("answer-question"),
  answerForm: document.getElementById("answer-form"),
  answerInput: document.getElementById("answer-input"),
  submitAnswer: document.getElementById("submit-answer"),
  answerMsg: document.getElementById("answer-msg"),
  answerDone: document.getElementById("answer-done"),
  answerText: document.getElementById("answer-text"),
};

let firstLoad = true;
let assignmentVersionShown = null; // 当前页面展示的（也是提交时绑定的）稿件版本
let credentialShown;               // 页面当前展示分配的凭据（undefined 表示尚未载入）
let credentialBound = null;        // 提交意见时绑定的分配凭据（null 表示分配从未改变，沿用原提交格式）
let reassignPending = false;       // 已发现分配变更、等待确认重读

function setMsg(el, text, isError) {
  el.textContent = text || "";
  el.className = isError ? "error" : "ok";
}

async function refresh() {
  let s;
  try {
    const r = await fetch("/api/p/" + token);
    const data = await r.json();
    if (!r.ok) {
      document.getElementById("content").hidden = true;
      const fatal = document.getElementById("fatal");
      fatal.hidden = false;
      fatal.textContent = data.error || "个人链接无效";
      return;
    }
    s = data;
  } catch {
    return; // 网络抖动，下次轮询再试
  }
  render(s);
}

function render(s) {
  els.name.textContent = s.name;
  els.version.textContent = "v" + s.version;

  // 只有自己能改自己的稿，首次载入填充即可，避免轮询覆盖正在输入的内容
  if (firstLoad) {
    els.manuscript.value = s.manuscript;
    if (s.my_review) els.review.value = s.my_review.content;
  }

  // 被分配的稿件：分配改变（有成员退出）时保留未交文本、禁用提交，确认重读后才绑定新凭据；
  // 同一分配的稿件版本变化时刷新展示并提示重新阅读
  const cred = s.assignment.credential || null;
  if (credentialShown === undefined) {
    // 首次载入：展示并直接绑定当前分配
    credentialShown = cred;
    credentialBound = cred;
    assignmentVersionShown = s.assignment.version;
    els.assignment.textContent = s.assignment.manuscript;
    els.assignmentVersion.textContent = "v" + s.assignment.version;
  } else if (cred !== credentialShown) {
    // 分配改变：即使新旧稿件版本号相同也必须刷新展示，并等待确认重读
    credentialShown = cred;
    assignmentVersionShown = s.assignment.version;
    els.assignment.textContent = s.assignment.manuscript;
    els.assignmentVersion.textContent = "v" + s.assignment.version;
    reassignPending = true;
  } else if (assignmentVersionShown !== s.assignment.version) {
    assignmentVersionShown = s.assignment.version;
    els.assignment.textContent = s.assignment.manuscript;
    els.assignmentVersion.textContent = "v" + s.assignment.version;
    els.assignmentNotice.hidden = !s.published;
  }
  // 确认重读前禁用提交（未交文本保留在输入框中，轮询不触碰）；公布后整体锁定
  els.reassignNotice.hidden = !(reassignPending && !s.published);
  els.submitReview.disabled = s.published || reassignPending;

  // 我的意见状态
  if (!s.my_review) {
    els.reviewStatus.textContent = "尚未提交意见。";
    els.reviewStatus.className = "muted";
  } else if (s.my_review.valid) {
    els.reviewStatus.textContent = "已提交（对应稿件 v" + s.my_review.version + "，当前有效，可修改后重新提交）。";
    els.reviewStatus.className = "ok";
  } else {
    els.reviewStatus.textContent =
      "你提交的意见（对应稿件 v" + s.my_review.version +
      "）已因稿件更新而失效，请阅读最新稿件后重新提交。";
    els.reviewStatus.className = "pending";
  }

  // 公布后锁定一切编辑，并展示收到的匿名意见
  if (s.published) {
    els.publishedBanner.hidden = false;
    els.assignmentNotice.hidden = true;
    els.manuscript.disabled = true;
    els.saveMs.disabled = true;
    els.review.disabled = true;
    els.submitReview.disabled = true;
    els.receivedSection.hidden = false;
    els.received.textContent = s.received !== null ? s.received : "（无）";
  }

  renderQuestion(s.published ? s.received_clarification : null);
  renderAnswer(s.published ? s.my_review_clarification : null);

  firstLoad = false;
}

// 作者视角：对收到的匿名意见的一问一答
function renderQuestion(c) {
  // 尚未提问：仅在状态位允许时保持输入框可编辑；轮询不触碰输入框，避免覆盖正在输入的内容
  if (!c) {
    els.questionForm.hidden = false;
    els.questionDone.hidden = true;
    return;
  }
  // 一旦问题已提交（本地或其他标签页）：锁定为只读问答，输入框整体隐藏
  els.questionForm.hidden = true;
  els.questionDone.hidden = false;
  els.questionText.textContent = c.question;
  if (c.answered) {
    els.answerView.textContent = "";
    els.answerView.className = "clarify-item";
    els.answerView.textContent = "对方答复：" + c.answer;
  } else {
    els.answerView.className = "muted";
    els.answerView.textContent = "已提交，等待对方答复。";
  }
}

// 评审者视角：作者对我所交意见的提问与我的答复
function renderAnswer(c) {
  if (!c) {
    els.answerBox.hidden = true;
    return;
  }
  els.answerBox.hidden = false;
  // 问题原文只读，直接以服务端快照展示
  els.answerQuestion.textContent = c.question;
  if (c.answered) {
    // 答复已提交：锁定只读，隐藏输入区（答复内容以服务端快照为准）
    els.answerForm.hidden = true;
    els.answerDone.hidden = false;
    els.answerText.textContent = c.answer;
  } else {
    // 尚无答复：显示输入区，但轮询不触碰输入框 value，避免覆盖正在输入的答复
    els.answerForm.hidden = false;
    els.answerDone.hidden = true;
  }
}

els.saveMs.addEventListener("click", async () => {
  setMsg(els.msMsg, "");
  try {
    const r = await fetch("/api/p/" + token + "/manuscript", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manuscript: els.manuscript.value }),
    });
    const data = await r.json();
    if (!r.ok) {
      setMsg(els.msMsg, data.error || "保存失败", true);
    } else {
      setMsg(els.msMsg, "已保存为 v" + data.version + "，针对你稿件的已交意见已失效。");
    }
  } catch {
    setMsg(els.msMsg, "网络错误，请重试", true);
  }
  refresh();
});

// 确认重新阅读新分配的稿件：绑定当前（新）凭据后才允许提交
els.confirmReassign.addEventListener("click", () => {
  credentialBound = credentialShown;
  reassignPending = false;
  els.reassignNotice.hidden = true;
  els.submitReview.disabled = false;
  setMsg(els.reviewMsg, "已绑定新分配，可针对当前稿件提交意见。");
});

els.submitReview.addEventListener("click", async () => {
  setMsg(els.reviewMsg, "");
  try {
    const r = await fetch("/api/p/" + token + "/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: els.review.value,
        version: assignmentVersionShown, // 绑定当前读到的稿件版本
        credential: credentialBound,     // 绑定确认过的分配凭据（分配从未改变时为 null，等同原格式）
      }),
    });
    const data = await r.json();
    if (!r.ok) {
      setMsg(els.reviewMsg, data.error || "提交失败", true);
    } else {
      setMsg(els.reviewMsg, "意见已提交。");
      els.assignmentNotice.hidden = true;
    }
  } catch {
    setMsg(els.reviewMsg, "网络错误，请重试", true);
  }
  refresh();
});

async function submitOnce(btn, path, payload, msgEl, okText) {
  btn.disabled = true;
  setMsg(msgEl, "");
  try {
    const r = await fetch("/api/p/" + token + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) {
      btn.disabled = false;
      setMsg(msgEl, data.error || "提交失败", true);
      return false;
    }
    setMsg(msgEl, okText);
    return true;
  } catch {
    btn.disabled = false;
    setMsg(msgEl, "网络错误，请重试", true);
    return false;
  }
}

els.submitQuestion.addEventListener("click", async () => {
  const ok = await submitOnce(
    els.submitQuestion,
    "/question",
    { question: els.questionInput.value },
    els.questionMsg,
    "问题已提交，等待对方答复。"
  );
  refresh(); // 成功后用快照锁定为只读；失败也刷新以发现其他标签页的并发提交
  if (ok) els.questionInput.value = "";
});

els.submitAnswer.addEventListener("click", async () => {
  const ok = await submitOnce(
    els.submitAnswer,
    "/answer",
    { answer: els.answerInput.value },
    els.answerMsg,
    "答复已提交。"
  );
  refresh(); // 成功后用快照锁定为只读；失败也刷新以发现其他标签页的并发提交
  if (ok) els.answerInput.value = "";
});

refresh();
setInterval(refresh, 5000);
