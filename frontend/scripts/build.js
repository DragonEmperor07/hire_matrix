const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const dist = path.join(root, "dist");
const apiBase = process.env.HIREMATRIX_API_BASE || "";

fs.rmSync(dist, { recursive: true, force: true });
fs.mkdirSync(dist, { recursive: true });
fs.cpSync(path.join(root, "index.html"), path.join(dist, "index.html"));
fs.cpSync(path.join(root, "static"), path.join(dist, "static"), { recursive: true });

fs.writeFileSync(
  path.join(dist, "config.js"),
  `window.HIREMATRIX_API_BASE = ${JSON.stringify(apiBase.replace(/\/$/, ""))};\n`
);
