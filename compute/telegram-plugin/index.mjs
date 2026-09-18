import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const HELPER = join(homedir(), ".local/share/compute-cluster/app/telegram_control.py");
const DENIED = "Cluster control is available only to the verified owner in this bot's private chat.";

export function currentOwner(config) {
  const entries = config?.commands?.ownerAllowFrom;
  if (!Array.isArray(entries)) return null;
  const owners = entries.filter(value => typeof value === "string" && /^telegram:[1-9][0-9]{0,19}$/.test(value));
  if (owners.length !== 1) return null;
  return owners[0].slice("telegram:".length);
}

function bounded(text) {
  let output = text.slice(0, 3400);
  if (/[\uD800-\uDBFF]$/.test(output)) output = output.slice(0, -1);
  return output;
}

export function invokeHelper(args) {
  return new Promise((resolve, reject) => {
    const child = execFile("/usr/bin/python3", [HELPER], {
      timeout: 10000,
      maxBuffer: 64 * 1024,
      encoding: "utf8",
      windowsHide: true,
    }, (error, stdout) => {
      if (error) return reject(new Error("Cluster helper did not finish"));
      try {
        const result = JSON.parse(stdout);
        if (!result || typeof result.text !== "string") throw new Error("Invalid helper response");
        resolve({ text: bounded(result.text), ...(result.isError === true ? { isError: true } : {}) });
      } catch {
        reject(new Error("Invalid cluster helper response"));
      }
    });
    // Neither command text nor requester identifiers enter an OS command line.
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ args }));
  });
}

export function createHandler(ownerId, invoke = invokeHelper) {
  return async (ctx) => {
    const owner = typeof ownerId === "string" && /^[1-9][0-9]{0,19}$/.test(ownerId) ? ownerId : null;
    if (!owner || currentOwner(ctx.config) !== owner || ctx.channel !== "telegram" ||
        (ctx.channelId != null && ctx.channelId !== "telegram") ||
        ctx.isAuthorizedSender !== true || ctx.senderIsOwner !== true || ctx.senderId !== owner ||
        ctx.from !== `telegram:${owner}` || ctx.to !== `telegram:${owner}` || ctx.threadParentId != null) {
      return { text: DENIED, continueAgent: false };
    }
    const args = ctx.args ?? "";
    if (typeof args !== "string" || args.includes("\0") || Buffer.byteLength(args, "utf8") > 16 * 1024) {
      return { text: "Cluster command is invalid or too long.", isError: true, continueAgent: false };
    }
    try {
      const result = await invoke(args);
      return { text: bounded(result.text), ...(result.isError === true ? { isError: true } : {}), continueAgent: false };
    } catch {
      return {
        text: "Cluster is unavailable or the request did not finish. A job may already be queued; check /cluster status before repeating it.",
        isError: true,
        continueAgent: false,
      };
    }
  };
}

export default {
  id: "compute-cluster-control",
  name: "Compute Cluster Control",
  register(api) {
    const ownerId = api.pluginConfig?.ownerId;
    if (typeof ownerId !== "string" || currentOwner(api.config) !== ownerId) {
      throw new Error("Compute cluster control requires its private ownerId to match the current Telegram commands.ownerAllowFrom entry");
    }
    api.registerCommand({
      name: "cluster",
      description: "Control your private coding and compute cluster",
      channels: ["telegram"],
      acceptsArgs: true,
      requireAuth: true,
      // OpenClaw exposes senderIsOwner to scoped external commands, and requires
      // owner authority for chat callers without gateway operator scopes.
      requiredScopes: ["operator.admin"],
      handler: createHandler(ownerId),
    });
  },
};
