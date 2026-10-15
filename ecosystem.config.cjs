const fs = require("fs");
const path = require("path");

function loadEnv(file) {
  const env = {};
  if (!fs.existsSync(file)) return env;
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
    const i = trimmed.indexOf("=");
    const key = trimmed.slice(0, i).trim();
    const value = trimmed.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    env[key] = value;
  }
  return env;
}

const root = __dirname;
const fileEnv = loadEnv(path.join(root, ".env"));

module.exports = {
  apps: [
    {
      name: "tender-dnl",
      cwd: root,
      script: path.join(root, "venv/bin/python"),
      args: "dashboard.py",
      interpreter: "none",
      env: {
        ...fileEnv,
        PLAYWRIGHT_BROWSERS_PATH: "/root/.cache/ms-playwright",
      },
    },
  ],
};
