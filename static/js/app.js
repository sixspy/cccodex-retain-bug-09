(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁" || state === "一致"
          ? "ok"
          : state === "已失败" || state === "已作废" || state === "在用版缺失"
            ? "bad"
            : state && state.includes("待重扫")
              ? "bad"
              : state === "裁撤中" || state === "执行中"
                ? "warn"
                : "";
    return `<span class="tag ${cls}">${esc(state)}</span>`;
  }

  function errBox(err) {
    return `<p class="err">${esc(err.message || err)}</p>`;
  }

  function actionable(j) {
    return j.status === "待执行" || j.status === "已失败";
  }

  function bindingCell(j) {
    const bound = `绑定 v${j.policy_version} / ${j.keep_days}天 / ${esc(j.decided_action)}`;
    if (j.binding_state === "已收口") {
      return `<span class="tag">已收口</span><div class="micro">${bound}</div>`;
    }
    if (j.binding_state === "一致") {
      return `<span class="tag ok">一致</span><div class="micro">${bound}</div>`;
    }
    return `${tag(j.binding_state)}<div class="micro">${bound}</div>`;
  }

  function liveCell(j) {
    if (j.live_version == null) return `<span class="tag bad">在用版缺失</span>`;
    return `<div class="mono">v${j.live_version} / ${j.live_keep_days}天 / ${esc(j.live_action)}</div>`;
  }

  async function renderPolicies() {
    const [policies, versions] = await Promise.all([
      api("/api/policies"),
      api("/api/policy-versions"),
    ]);
    const board = new Map(versions.board.map((b) => [b.domain_code, b]));
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略（在用版）</h2>
        <p class="sub">每个主题域一条现行版本；右侧「开单绑定版」是尚未收口的裁决实际绑定的快照，两者分列核对。</p>
        <table>
          <thead><tr>
            <th>主题域</th><th>编码</th><th>在用版</th>
            <th>在用保留天</th><th>在用到期动作</th>
            <th>开单绑定版</th><th>待收口/待重扫</th>
          </tr></thead>
          <tbody>
            ${policies.items
              .map((p) => {
                const b = board.get(p.domain_code) || { bound_versions: [], open_total: 0, stale_open: 0 };
                const bound = b.bound_versions.length
                  ? b.bound_versions
                      .map(
                        (v) =>
                          `<div class="mono">v${v.policy_version} / ${v.keep_days}天 / ${esc(v.decided_action)} · ${v.c} 条${
                            v.policy_version === p.version ? "" : ' <span class="tag bad">落后在用版</span>'
                          }</div>`
                      )
                      .join("")
                  : `<span class="empty">无开放裁决</span>`;
                return `<tr>
              <td>${esc(p.domain_name)}</td>
              <td class="mono">${esc(p.domain_code)}</td>
              <td><span class="tag ok">在用 v${p.version}</span></td>
              <td>${p.keep_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>${bound}</td>
              <td>${b.open_total} 条 / 待重扫 <strong>${b.stale_open}</strong></td>
            </tr>`;
              })
              .join("")}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <h2>策略版本队列</h2>
        <p class="sub">版本按序排队；active 为在用版。换版不直接改写已开裁决。处置口径已择定：<strong>重扫</strong>（不锁旧）——旧绑定单由下一次到期扫描作废并按新快照重开。</p>
        <table>
          <thead><tr><th>主题域</th><th>版本</th><th>保留天</th><th>到期动作</th><th>状态</th><th>变更说明</th><th>生效时间</th></tr></thead>
          <tbody>
            ${versions.items
              .map(
                (v) => `<tr>
              <td class="mono">${esc(v.domain_code)}</td>
              <td><span class="mono">v${v.version}</span></td>
              <td>${v.keep_days}</td>
              <td>${tag(v.action_on_expire)}</td>
              <td>${v.active === 1 ? '<span class="tag ok">在用版</span>' : '<span class="tag">历史版</span>'}</td>
              <td>${esc(v.change_note)}</td>
              <td class="mono">${esc(v.created_at)}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <h2>发布新版本（换版）</h2>
        <p class="sub">缩天/延期/动作变更都会产生新版本；已开裁决仍显示原绑定快照，需重跑到期扫描按重扫口径收口。</p>
        <div class="toolbar">
          <label class="field">主题域
            <select id="pub-domain">
              ${policies.items
                .map(
                  (p) =>
                    `<option value="${esc(p.domain_code)}">${esc(p.domain_name)}（v${p.version} / ${p.keep_days}天 / ${esc(p.action_on_expire)}）</option>`
                )
                .join("")}
            </select>
          </label>
          <label class="field">新保留天
            <input id="pub-keep" type="number" min="1" value="7" />
          </label>
          <label class="field">到期动作
            <select id="pub-action"><option>归档</option><option>销毁</option></select>
          </label>
          <label class="field">变更说明
            <input id="pub-note" placeholder="例如：缩天 7→3" />
          </label>
          <button type="button" id="btn-publish">发布新版本</button>
        </div>
        <div id="pub-msg"></div>
      </section>`;

    const domainSelect = document.getElementById("pub-domain");
    domainSelect.onchange = () => {
      const p = policies.items.find((x) => x.domain_code === domainSelect.value);
      if (!p) return;
      document.getElementById("pub-keep").value = p.keep_days;
      document.getElementById("pub-action").value = p.action_on_expire;
    };
    document.getElementById("btn-publish").onclick = async () => {
      const msg = document.getElementById("pub-msg");
      try {
        const result = await api("/api/policies/publish", {
          method: "POST",
          body: JSON.stringify({
            domain_code: domainSelect.value,
            keep_days: Number(document.getElementById("pub-keep").value),
            action_on_expire: document.getElementById("pub-action").value,
            change_note: document.getElementById("pub-note").value,
          }),
        });
        await renderPolicies();
        document.getElementById("pub-msg").innerHTML = `<p class="okline">已发布 v${result.current.version}（${esc(
          result.direction
        )}）；该域仍有 ${result.stale_open_judgments} 条旧绑定开放裁决待重扫。</p>`;
      } catch (err) {
        msg.innerHTML = errBox(err);
      }
    };
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    main.innerHTML = `
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。数字与后续对账同源。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>归档地址</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${esc(p.domain_code)}</td>
              <td class="mono">${esc(p.biz_date)}</td>
              <td>${p.row_count}</td>
              <td>${tag(p.storage_state)}</td>
              <td class="mono">${esc(p.archive_uri || "—")}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <div id="scan-msg"></div>
      <section class="panel">
        <h2>扫描任务</h2>
        <p class="sub">扫描任务持「进行中」占坑锁写；摘要按开单/重扫/作废/占用分列，与裁决详情的绑定快照同源。</p>
        <table>
          <thead><tr>
            <th>编号</th><th>基准日</th><th>状态</th><th>到期</th><th>开单</th>
            <th>重扫重开</th><th>作废</th><th>占用跳过</th><th>说明</th><th>详情</th>
          </tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${esc(j.as_of_date)}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${j.opened_count}</td>
              <td>${j.rescanned_count}</td>
              <td>${j.revoked_count}</td>
              <td>${j.skipped_open_count}</td>
              <td>${esc(j.message)}</td>
              <td><button type="button" class="secondary" data-detail="${j.id}">查看</button></td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="10" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>
      <div id="scan-detail"></div>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      const msg = document.getElementById("scan-msg");
      try {
        await api("/api/scan-jobs", {
          method: "POST",
          body: JSON.stringify({ as_of_date }),
        });
        await renderScan();
      } catch (err) {
        msg.innerHTML = errBox(err);
      }
    };
    main.querySelectorAll("[data-detail]").forEach((btn) => {
      btn.onclick = async () => {
        const { job } = await api(`/api/scan-jobs/${btn.dataset.detail}`);
        document.getElementById("scan-detail").innerHTML = `
          <section class="panel">
            <h2>扫描 #${job.id} 编排明细</h2>
            <p class="sub">逐分区记录：在用版、绑定版、龄期与处置（新开单 / 重扫重开 / 重扫作废 / 占用跳过）。${
              job.binding_summary ? "换版摘要：" + esc(job.binding_summary) : ""
            }</p>
            <table>
              <thead><tr>
                <th>主题域</th><th>业务日</th><th>龄期</th>
                <th>在用版</th><th>绑定版</th><th>处置</th>
              </tr></thead>
              <tbody>
                ${
                  job.detail.length
                    ? job.detail
                        .map(
                          (d) => `<tr>
                  <td class="mono">${esc(d.domain_code)}</td>
                  <td class="mono">${esc(d.biz_date)}</td>
                  <td>${d.age_days}天</td>
                  <td class="mono">v${d.live_version} / ${d.live_keep_days}天 / ${esc(d.live_action)}</td>
                  <td class="mono">v${d.bound_version} / ${d.bound_keep_days}天</td>
                  <td>${tag(d.outcome)}</td>
                </tr>`
                        )
                        .join("")
                    : `<tr><td colspan="6" class="empty">该次扫描没有逐分区动作</td></tr>`
                }
              </tbody>
            </table>
          </section>`;
      };
    });
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    const stale = data.items.filter(
      (j) => actionable(j) && j.live_version != null && j.policy_version !== j.live_version
    );
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">「绑定快照」为开单当时锁定；右侧「在用版」独立展示，二者不一致时需先重跑到期扫描按重扫口径处置，不能只改文案。</p>
        ${
          stale.length
            ? `<p class="warnline">有 ${stale.length} 条开放裁决绑定版本落后于在用版（缩天/延期/动作变更），执行会被拒绝；请到「到期扫描」重跑。</p>`
            : ""
        }
        <div id="jdg-msg"></div>
        <table>
          <thead><tr>
            <th>编号</th><th>主题域</th><th>业务日</th>
            <th>绑定快照</th><th>在用版</th><th>绑定核对</th>
            <th>动作</th><th>状态</th><th>处置</th><th>备注</th><th>操作</th>
          </tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map((j) => {
                      const canRun =
                        actionable(j) &&
                        (j.live_version == null || j.policy_version === j.live_version);
                      return `<tr>
              <td>${j.id}</td>
              <td class="mono">${esc(j.domain_code)}</td>
              <td class="mono">${esc(j.biz_date)}</td>
              <td class="mono">v${j.policy_version} / ${j.keep_days}天</td>
              <td>${liveCell(j)}</td>
              <td>${bindingCell(j)}</td>
              <td>${tag(j.decided_action)}</td>
              <td>${tag(j.status)}</td>
              <td>${esc(j.disposition)}</td>
              <td class="note">${esc(j.note || j.fail_reason || "—")}${
                        j.supersedes_id ? `<div class="micro">取代 #${j.supersedes_id}</div>` : ""
                      }${j.superseded_by_id ? `<div class="micro">已由 #${j.superseded_by_id} 取代</div>` : ""}</td>
              <td>${
                canRun
                  ? `<button type="button" data-exec="${j.id}">执行</button>`
                  : actionable(j)
                    ? `<button type="button" class="secondary" disabled title="绑定版已落后，请先重扫">待重扫</button>`
                    : "—"
              }</td>
            </tr>`;
                    })
                    .join("")
                : `<tr><td colspan="11" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
        } catch (err) {
          document.getElementById("jdg-msg").innerHTML = errBox(err);
        }
        await renderJudgments();
      };
    });
  }

  async function renderRuns() {
    const data = await api("/api/runs");
    main.innerHTML = `
      <section class="panel">
        <h2>执行流水</h2>
        <p class="sub">每条流水都回带裁决的开单绑定快照（版本 / 保留天），执行口径可回溯。</p>
        <table>
          <thead><tr>
            <th>流水号</th><th>裁决</th><th>主题域</th><th>业务日</th>
            <th>绑定快照</th><th>动作</th><th>结果态</th><th>归档地址</th><th>成败</th><th>说明</th>
          </tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td>#${r.judgment_id}</td>
              <td class="mono">${esc(r.domain_code)}</td>
              <td class="mono">${esc(r.biz_date)}</td>
              <td class="mono">v${r.policy_version} / ${r.keep_days}天</td>
              <td>${tag(r.action)}</td>
              <td>${tag(r.result_state)}</td>
              <td class="mono">${esc(r.archive_uri || "—")}</td>
              <td>${r.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}</td>
              <td class="note">${esc(r.detail)}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="10" class="empty">尚无执行流水</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <p class="sub">实物错位与「绑定版落后待重扫」分开统计：前者是已完成裁决与分区状态不符，后者是尚未按新版本重扫的开放裁决。</p>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>错位</th><th>摘要</th></tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${esc(r.as_of_date)}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.mismatch_count}</td>
              <td>${esc(r.summary)}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      await api("/api/reconcile-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderReconcile();
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    runs: renderRuns,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    main.innerHTML = `<p class="empty">加载中…</p>`;
    try {
      await renderers[name]();
    } catch (err) {
      main.innerHTML = errBox(err);
    }
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();
