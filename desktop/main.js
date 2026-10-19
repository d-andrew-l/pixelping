// PixelPing desktop shell: makes sure pixelping is running on the server, opens an
// ssh tunnel to its dashboard, and shows it in an app window. No dependencies;
// run with any Electron binary: electron.exe path\to\pixelping\desktop
// Set PIXELPING_SSH to the server's ssh alias (and PIXELPING_DIR if pixelping is not in ~/pixelping).
const { app, BrowserWindow } = require("electron");
const { spawn, execFile } = require("child_process");
const http = require("http");
const path = require("path");

const SERVER = process.env.PIXELPING_SSH;                // ssh alias from ~/.ssh/config
const REMOTE_DIR = process.env.PIXELPING_DIR || "~/pixelping";
const PORT = 8765;                                    // same port locally and remotely
const URL = `http://127.0.0.1:${PORT}/`;
const SSH = process.platform === "win32" ? "C:\\Windows\\System32\\OpenSSH\\ssh.exe" : "ssh";
const SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"];
const ICON = path.join(__dirname, "pixelping.ico");

let win, tunnel;

if (!app.requestSingleInstanceLock()) app.quit();
app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });

function page(title, body) {
  const html = `<!doctype html><meta charset="utf-8"><body style="margin:0;height:100vh;display:grid;
    place-items:center;background:#241a33;color:#e8e0f0;font:15px system-ui;text-align:center">
    <div><h2 style="margin:0 0 8px">${title}</h2><pre style="white-space:pre-wrap;opacity:.75;
    max-width:80vw">${body.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c])}</pre></div>`;
  win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
}

// Start pixelping on the server unless something already listens on its port.
// It is detached there, so it keeps collecting history after this window closes.
function ensureServer() {
  const cmd = `cd ${REMOTE_DIR} && mkdir -p logs && ` +
    `(ss -ltnH 'sport = :${PORT}' | grep -q . || ` +
    `(setsid nohup python3 pixelping.py >>logs/server.log 2>&1 </dev/null &))`;
  return new Promise((resolve, reject) => {
    execFile(SSH, [...SSH_OPTS, SERVER, cmd], { windowsHide: true, timeout: 20000 },
      (err, _out, stderr) => (err ? reject(new Error(stderr || err.message)) : resolve()));
  });
}

function openTunnel() {
  let stderr = "";
  tunnel = spawn(SSH, [...SSH_OPTS, "-N", "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=15", "-L", `${PORT}:127.0.0.1:${PORT}`, SERVER],
    { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });
  tunnel.stderr.on("data", (d) => { stderr += d; });
  tunnel.on("exit", (code) => {
    tunnel = null;
    if (win && !app.isQuitting) page("Tunnel to " + SERVER + " closed", stderr || `ssh exited (${code})`);
  });
}

function waitForDashboard(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get(URL, (res) => { res.resume(); res.statusCode === 200 ? resolve() : retry(); });
      req.on("error", retry);
      req.setTimeout(2000, () => req.destroy());
    };
    const retry = () => {
      if (!tunnel) return reject(new Error("ssh tunnel exited"));
      if (Date.now() > deadline) return reject(new Error("dashboard did not answer at " + URL));
      setTimeout(attempt, 400);
    };
    attempt();
  });
}

async function connect() {
  if (!SERVER) return page("PixelPing", "Set PIXELPING_SSH to the ssh alias of the server running pixelping, then start again.");
  page("PixelPing", `connecting to ${SERVER}…`);
  try {
    await ensureServer();
    openTunnel();
    await waitForDashboard();
    win.loadURL(URL);
  } catch (e) {
    if (!app.isQuitting) page("Could not reach pixelping on " + SERVER, String(e.message || e));
  }
}

app.setAppUserModelId("pixelping.desktop");
app.whenReady().then(() => {
  win = new BrowserWindow({
    width: 1400, height: 900, title: "PixelPing", icon: ICON,
    backgroundColor: "#241a33", autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  win.on("page-title-updated", (e) => e.preventDefault());
  win.webContents.on("before-input-event", (_e, input) => {
    if (input.type === "keyDown" && (input.key === "F5" || (input.control && input.key.toLowerCase() === "r"))) {
      tunnel ? win.webContents.reload() : connect();
    }
  });
  connect();
});

app.on("before-quit", () => { app.isQuitting = true; if (tunnel) tunnel.kill(); });
app.on("window-all-closed", () => app.quit());
