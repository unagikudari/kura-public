// kura_send: right-click a node that shows images → send the chosen one(s) to kura. See ../__init__.py.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

function imagesOf(node) {
  const out = app.nodeOutputs?.[node.id]?.images ?? node.images ?? [];
  return out.filter((i) => i && i.filename);
}

function say(severity, detail) {
  const toast = app.extensionManager?.toast;
  if (toast?.add) toast.add({ severity, summary: "kura", detail, life: 5000 });
  else if (severity === "error") alert(detail);
}

async function send(images) {
  try {
    const res = await api.fetchApi("/kura_send/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ images }) });
    const d = await res.json();
    if (d.sent?.length) say("success", `${d.sent.length} 枚を kura に送りました（1〜2 分で登録されます）`);
    if (d.refused?.length || !res.ok) say("error", `送れませんでした: ${(d.refused || []).join(", ") || res.status}`);
  } catch (e) { say("error", `送れませんでした: ${e}`); }
}

function menuItems(node) {
  const images = imagesOf(node);
  if (!images.length) return [];
  const index = node.imageIndex ?? (images.length === 1 ? 0 : null);
  const items = [];
  if (index != null && images[index]) items.push({ content: "kura に送る（表示中の 1 枚）", callback: () => send([images[index]]) });
  if (images.length > 1) items.push({ content: `kura に送る（このノードの ${images.length} 枚すべて）`, callback: () => send(images) });
  return items;
}

// The button: the one way that does not depend on which context menu the page version shows.
function pressed(node) {
  const images = imagesOf(node);
  if (!images.length) return say("warn", "このノードにはまだ画像がありません（生成してから押してください）");
  const index = node.imageIndex ?? (images.length === 1 ? 0 : null);
  if (index != null && images[index]) return send([images[index]]);
  if (confirm(`このノードの ${images.length} 枚をすべて kura に送りますか？\n（1 枚だけ送るときは、その画像をクリックして大きく表示してから押してください）`)) send(images);
}

const WITH_BUTTON = new Set(["SaveImage", "PreviewImage"]);

app.registerExtension({
  name: "kura.send",
  nodeCreated(node) {
    if (!WITH_BUTTON.has(node.comfyClass)) return;
    node.addWidget("button", "kura に送る", null, () => pressed(node), { serialize: false });
  },
  // page versions with the new context menu ask here
  getNodeMenuItems(node) {
    return menuItems(node);
  },
  // older page versions read the node prototype
  async beforeRegisterNodeDef(nodeType) {
    const original = nodeType.prototype.getExtraMenuOptions;
    nodeType.prototype.getExtraMenuOptions = function (canvas, options) {
      const r = original?.apply(this, arguments);
      const mine = menuItems(this);
      if (mine.length && !options.some((o) => o?.content?.startsWith?.("kura に送る"))) options.unshift(...mine, null);
      return r;
    };
  },
});
