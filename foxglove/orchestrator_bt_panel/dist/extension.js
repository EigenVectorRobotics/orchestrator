// Foxglove extension: the orchestrator behavior tree from /orchestrator/trace (std_msgs/String, one JSON event per message).
// No build step: plain CommonJS and DOM. The tree comes from the "started" event (its XML); node paths follow the executor:
// <tree id>/<root tag>, then [i]/<tag> for child i; a SubTree adds /<its root tag>.
"use strict";

const TRACE_TOPIC = "/orchestrator/trace";
const REQUEST_TOPIC = "/orchestrator/agent/request";     // latched JSON: the open agent call (id, skill, task, decisions)
const REPLY_TOPIC = "/orchestrator/agent/reply";         // JSON: accepted or not, with a message
const DECISION_TOPIC = "/orchestrator/agent/decision";   // JSON: id of the open call, decision, reason
const BUTTON_SKILLS = ["operator_confirm"];              // other skills need more fields than a button gives (agent_cli)
const SKIP_ATTRS = ["name", "_description"];
const STATUS_COLOR = { success: "#2e9e44", failure: "#d43c3c", error: "#d43c3c", running: "#d9a400", idle: "#888" };
const LOG_KINDS = ["task", "reuse", "plan", "started", "finished", "agent_wait", "agent_decision", "preflight", "error"];

function stamp(time) {
  return time.sec + time.nsec * 1e-9;
}

function parseEvent(messageEvent) {
  try {
    const event = JSON.parse(messageEvent.message.data);
    event._t = stamp(messageEvent.receiveTime);
    return event;
  } catch (error) {
    return null;
  }
}

function buildTree(trees, id, path, visiting, byPath) {
  const root = trees[id] && trees[id].children[0];
  if (!root || visiting.includes(id)) return null;
  return buildNode(trees, root, `${path}/${root.tagName}`, [...visiting, id], byPath);
}

function buildNode(trees, el, path, visiting, byPath) {
  if (el.tagName === "SubTree") {
    const id = el.getAttribute("ID");
    const node = { kind: "sub", label: id, path: path, children: [] };
    const child = buildTree(trees, id, path, visiting, byPath);
    if (child) node.children.push(child);
    byPath[path] = node;
    return node;
  }
  const attrs = [...el.attributes].filter((a) => !SKIP_ATTRS.includes(a.name)).map((a) => `${a.name}=${a.value}`).join(" ");
  const node = { kind: el.children.length ? "ctl" : "leaf", label: el.tagName, attrs: attrs, path: path, children: [] };
  byPath[path] = node;
  [...el.children].forEach((c, i) => node.children.push(buildNode(trees, c, `${path}[${i}]/${c.tagName}`, visiting, byPath)));
  return node;
}

function newRun(event) {
  const doc = new DOMParser().parseFromString(event.tree || "<root/>", "application/xml");
  const trees = {};
  doc.querySelectorAll("BehaviorTree").forEach((t) => { trees[t.getAttribute("ID")] = t; });
  const byPath = {};
  const id = event.tree_id || Object.keys(trees)[0];
  const root = trees[id] ? buildTree(trees, id, id, [], byPath) : null;
  return { id: id, runId: event.run_id, start: event._t, end: null, status: "running", root: root, byPath: byPath,
           history: {}, cur: null, lastSay: "", log: [] };
}

// A run joined after its "started" event (the trace is volatile): the tree is built from the node paths seen
function pathRun(event) {
  return { id: event.tree_id || "?", runId: event.run_id, start: event._t, end: null, status: "running", byPath: {},
           root: null, history: {}, cur: null, lastSay: "", log: [{ t: event._t, cls: "info", text: "joined mid-run: the tree shows only the nodes seen" }] };
}

function addPath(run, path) {
  const segs = path.split("/");
  let parent = null;
  for (let k = 1; k < segs.length; k++) {
    const key = segs.slice(0, k).join("/") + "/" + segs[k].replace(/\[\d+\]$/, "");
    let node = run.byPath[key];
    if (!node) {
      node = { kind: "ctl", label: segs[k].replace(/\[\d+\]$/, ""), attrs: "", path: key, children: [] };
      run.byPath[key] = node;
      if (parent) parent.children.push(node); else run.root = node;
    }
    parent = node;
  }
  if (parent && !parent.children.length) parent.kind = "leaf";
}

// Fold the events up to time t (all of them when t is null) into the state of the last run
function reduce(events, t) {
  let run = null;
  let partial = false;
  const planner = [];
  for (const event of events) {
    if (t !== null && event._t > t) break;
    if (event.kind === "started") { run = newRun(event); partial = false; }
    const path = event.kind === "record" ? event.record && event.record.path : event.path;
    const status = event.kind === "record" ? event.record && event.record.status : event.status;
    if ((event.kind === "node" || event.kind === "record") && path && event.run_id && (!run || run.runId !== event.run_id)) {
      run = pathRun(event);
      partial = true;
    }
    if (!run) {
      if (LOG_KINDS.includes(event.kind)) planner.push(event);
      continue;
    }
    run.lastT = event._t;
    if (partial && path) addPath(run, path);
    if ((event.kind === "node" || event.kind === "record") && path) {
      run.history[path] = { status: status, t: event._t };
      run.cur = path;
      if (event.kind === "record" && event.record.skill === "say" && event.record.inputs) {
        run.lastSay = event.record.inputs.text || "";
        run.log.push({ t: event._t, cls: "say", text: `says: ${run.lastSay}` });
      }
      // conditions fail all the time in reactive nodes: only failed actions go to the log
      if ((status === "failure" || status === "error") && event.kind === "record" && event.record.kind === "action") {
        const last = run.log[run.log.length - 1];
        if (!last || last.path !== path) run.log.push({ t: event._t, cls: "fail", text: event.text, path: path });
      }
    } else if (event.kind === "finished") {
      run.status = event.status || "finished";
      run.end = event._t;
      run.log.push({ t: event._t, cls: event.status === "success" ? "ok" : "fail", text: event.text });
    } else if (LOG_KINDS.includes(event.kind)) {
      run.log.push({ t: event._t, cls: "info", text: event.text });
    }
  }
  return { run: run, planner: planner };
}

function seen(run, node) {
  return !!run.history[node.path] || node.children.some((c) => seen(run, c));
}

function onCurrentPath(run, node) {
  return run.cur !== null && (run.cur === node.path || run.cur.startsWith(node.path + "/") || run.cur.startsWith(node.path + "["));
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderNode(run, node, depth, folds, container) {
  const h = run.history[node.path];
  const row = el("div", "row" + (run.cur === node.path ? " cur" : "") + (seen(run, node) ? "" : " unseen"));
  row.style.paddingLeft = `${depth * 14 + 4}px`;
  const open = folds[node.path] !== undefined ? folds[node.path] : (node.kind !== "sub" || onCurrentPath(run, node));
  const twisty = el("span", "twisty", node.children.length ? (open ? "▾" : "▸") : "");
  const dot = el("span", "dot");
  dot.style.background = h ? STATUS_COLOR[h.status] || "#888" : "transparent";
  dot.style.borderColor = h ? STATUS_COLOR[h.status] || "#888" : "#999";
  row.append(twisty, dot, el("span", node.kind === "sub" ? "label sub" : node.kind === "ctl" ? "label ctl" : "label leaf", node.label));
  if (node.attrs) row.append(el("span", "attrs", node.attrs));
  if (node.children.length) row.onclick = () => { folds[node.path] = !open; folds.__rerender(); };
  container.append(row);
  if (open) node.children.forEach((c) => renderNode(run, c, depth + 1, folds, container));
}

const CSS = `
.bt { font: 12px/1.45 ui-monospace, Menlo, Consolas, monospace; height: 100%; display: flex; flex-direction: column; color: var(--fg, #ddd); }
.bt .head { padding: 6px 8px; border-bottom: 1px solid #8884; }
.bt .head .title { font-weight: bold; font-size: 13px; }
.bt .badge { display: inline-block; padding: 0 6px; border-radius: 3px; color: #fff; margin-left: 6px; }
.bt .say { margin-top: 3px; opacity: .9; }
.bt .tree { flex: 1 1 60%; overflow: auto; padding: 4px 0; }
.bt .row { white-space: nowrap; cursor: default; }
.bt .row.unseen { opacity: .45; }
.bt .row.cur { background: #d9a40033; outline: 1px solid #d9a400; }
.bt .twisty { display: inline-block; width: 12px; cursor: pointer; }
.bt .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; border: 1px solid #999; margin: 0 5px 0 2px; }
.bt .label.sub { color: #5aa0ff; font-weight: bold; }
.bt .label.ctl { color: #b08cff; }
.bt .attrs { opacity: .7; margin-left: 6px; }
.bt .log { flex: 0 0 auto; max-height: 35%; overflow: auto; border-top: 1px solid #8884; padding: 4px 8px; }
.bt .log div { white-space: nowrap; }
.bt .log .fail { color: #ff6b6b; } .bt .log .ok { color: #4cc764; } .bt .log .say { color: #e0b84a; }
.bt .empty { padding: 12px; opacity: .7; }
.bt .ask { margin: 6px 8px; padding: 6px 8px; border: 1px solid #d9a400; border-radius: 4px; background: #d9a40022; white-space: normal; }
.bt .ask button { margin: 6px 6px 0 0; padding: 3px 10px; font: inherit; cursor: pointer; }
.bt .reply { margin-top: 4px; opacity: .85; }
`;

// The question for the operator from the call's task text, else the whole task
function question(request) {
  const line = (request.task || "").split("\n").find((l) => l.startsWith("Ask the operator"));
  return line || request.task || "";
}

function askBox(request, reply, canPublish, send) {
  const box = el("div", "ask");
  box.append(el("div", "", `Agent call ${request.id} (${request.skill || "?"}) is open: ${question(request)}`));
  if (canPublish && BUTTON_SKILLS.includes(request.skill)) {
    (request.decisions || []).forEach((decision) => {
      const button = el("button", "", decision);
      button.onclick = () => send(request, decision);
      box.append(button);
    });
  } else {
    box.append(el("div", "reply", canPublish ? "Answer it with agent_cli decide." : "Playback: no answer from here."));
  }
  if (reply && reply.id === request.id) box.append(el("div", "reply", `${reply.accepted ? "accepted" : "rejected"}: ${reply.message}`));
  return box;
}

function initPanel(context) {
  const style = el("style");
  style.textContent = CSS;
  const root = el("div", "bt");
  context.panelElement.append(style, root);
  const folds = {};
  let live = [];
  let all = null;
  let time = null;
  let last = null;
  let request = null;
  let reply = null;
  const canPublish = typeof context.advertise === "function" && typeof context.publish === "function";
  if (canPublish) context.advertise(DECISION_TOPIC, "std_msgs/msg/String");

  function send(call, decision) {
    const message = { id: call.id, decision: decision, reason: `Operator chose ${decision} in Foxglove.` };
    context.publish(DECISION_TOPIC, { data: JSON.stringify(message) });
  }

  function draw() {
    const events = all || live;
    const { run, planner } = reduce(events, all ? time : null);
    root.textContent = "";
    if (request && request.state === "open") root.append(askBox(request, reply, canPublish && !all, send));
    if (!run) {
      const empty = el("div", "empty", `No tree run yet on ${TRACE_TOPIC}.`);
      root.append(empty);
      planner.slice(-8).forEach((e) => root.append(el("div", "empty", e.text)));
      return;
    }
    const head = el("div", "head");
    const title = el("span", "title", run.id);
    const badge = el("span", "badge", run.status);
    badge.style.background = STATUS_COLOR[run.status] || "#888";
    const now = run.end || (all ? time : run.lastT);
    head.append(title, badge, el("span", "attrs", `${Math.max(0, now - run.start).toFixed(1)} s  run ${run.runId || ""}`));
    if (run.lastSay) head.append(el("div", "say", `“${run.lastSay}”`));
    if (run.cur) head.append(el("div", "attrs", `at ${run.cur.split("/").pop()}`));
    const tree = el("div", "tree");
    folds.__rerender = draw;
    if (run.root) renderNode(run, run.root, 0, folds, tree);
    const log = el("div", "log");
    run.log.slice(-60).forEach((entry) => log.append(el("div", entry.cls, `${(entry.t - run.start).toFixed(1).padStart(6)}  ${entry.text}`)));
    root.append(head, tree, log);
    const cur = tree.querySelector(".cur");
    if (cur) cur.scrollIntoView({ block: "nearest" });
    log.scrollTop = log.scrollHeight;
  }

  context.onRender = (renderState, done) => {
    for (const m of renderState.currentFrame || []) {
      if (m.topic !== REQUEST_TOPIC && m.topic !== REPLY_TOPIC) continue;
      try {
        if (m.topic === REQUEST_TOPIC) request = JSON.parse(m.message.data);
        else reply = JSON.parse(m.message.data);
      } catch (error) {
        // not JSON: keep the last one
      }
    }
    if (renderState.allFrames && renderState.allFrames.length) {
      if (renderState.allFrames !== last) {
        all = renderState.allFrames.filter((m) => m.topic === TRACE_TOPIC).map(parseEvent).filter(Boolean);
        last = renderState.allFrames;
      }
      time = renderState.currentTime ? stamp(renderState.currentTime) : null;
    } else if (renderState.currentFrame) {
      for (const m of renderState.currentFrame) {
        if (m.topic !== TRACE_TOPIC) continue;
        const event = parseEvent(m);
        if (event) live.push(event);
      }
      if (live.length > 20000) live = live.slice(-20000);
    }
    draw();
    done();
  };
  context.watch("currentFrame");
  context.watch("allFrames");
  context.watch("currentTime");
  context.subscribe([{ topic: TRACE_TOPIC, preload: true }, { topic: REQUEST_TOPIC }, { topic: REPLY_TOPIC }]);
  return () => { root.remove(); style.remove(); };
}

module.exports = {
  activate(extensionContext) {
    extensionContext.registerPanel({ name: "Behavior Tree", initPanel: initPanel });
  },
  reduce: reduce,
};
