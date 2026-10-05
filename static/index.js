"use strict";

const MIN = 3, MAX = 8;
const peopleEl = document.getElementById("people");
const errorEl = document.getElementById("error");

function renumber() {
  const rows = [...peopleEl.children];
  rows.forEach((el, i) => {
    el.querySelector(".person-title").textContent = "第 " + (i + 1) + " 位";
    el.querySelector(".remove").disabled = rows.length <= MIN;
  });
  document.getElementById("add-person").disabled = rows.length >= MAX;
}

function addPerson() {
  if (peopleEl.children.length >= MAX) return;
  const div = document.createElement("div");
  div.className = "card person";

  const head = document.createElement("div");
  head.className = "person-head";
  const title = document.createElement("strong");
  title.className = "person-title";
  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "remove link";
  removeBtn.textContent = "删除";
  removeBtn.addEventListener("click", () => { div.remove(); renumber(); });
  head.append(title, removeBtn);

  const nameLabel = document.createElement("label");
  nameLabel.textContent = "姓名";
  const nameInput = document.createElement("input");
  nameInput.className = "name";
  nameInput.maxLength = 50;
  nameInput.required = true;
  nameLabel.appendChild(nameInput);

  const textLabel = document.createElement("label");
  textLabel.textContent = "稿件（纯文本）";
  const textarea = document.createElement("textarea");
  textarea.className = "manuscript";
  textarea.rows = 5;
  textarea.required = true;
  textLabel.appendChild(textarea);

  div.append(head, nameLabel, textLabel);
  peopleEl.appendChild(div);
  renumber();
}

document.getElementById("add-person").addEventListener("click", addPerson);
for (let i = 0; i < MIN; i++) addPerson();

document.getElementById("create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.textContent = "";
  const people = [...peopleEl.children].map((el) => ({
    name: el.querySelector(".name").value.trim(),
    manuscript: el.querySelector(".manuscript").value,
  }));
  if (people.length < MIN || people.length > MAX) {
    errorEl.textContent = "需要 " + MIN + "～" + MAX + " 人";
    return;
  }
  if (people.some((p) => !p.name)) { errorEl.textContent = "姓名不能为空"; return; }
  if (new Set(people.map((p) => p.name)).size !== people.length) {
    errorEl.textContent = "姓名不能重复"; return;
  }
  if (people.some((p) => !p.manuscript.trim())) {
    errorEl.textContent = "稿件不能为空"; return;
  }

  let data;
  try {
    const resp = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ people }),
    });
    data = await resp.json();
    if (!resp.ok) { errorEl.textContent = data.error || "创建失败"; return; }
  } catch {
    errorEl.textContent = "网络错误，请重试";
    return;
  }

  const result = document.getElementById("result");
  result.hidden = false;
  const hostLink = document.getElementById("host-link");
  hostLink.href = location.origin + data.host_url;
  hostLink.textContent = location.origin + data.host_url;
  const tbody = document.getElementById("links-table");
  tbody.innerHTML = "";
  for (const p of data.people) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.textContent = p.name;
    const tdLink = document.createElement("td");
    const a = document.createElement("a");
    a.href = location.origin + p.url;
    a.textContent = location.origin + p.url;
    tdLink.appendChild(a);
    tr.append(tdName, tdLink);
    tbody.appendChild(tr);
  }
  result.scrollIntoView({ behavior: "smooth" });
});
