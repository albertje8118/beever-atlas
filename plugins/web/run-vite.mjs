import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.resolve(here, "../../web");
const workspaceDir = path.resolve(here, "../..");
const viteBin = path.resolve(webDir, "node_modules/vite/bin/vite.js");
const configPath = path.resolve(here, "vite.plugin.config.ts");
const rootEnvPath = path.resolve(workspaceDir, ".env");
const args = process.argv.slice(2);
const viteArgs = args.length > 0 ? args : ["dev"];
const env = { ...process.env };

delete env.VITE_API_URL;

if (fs.existsSync(rootEnvPath)) {
  const rootEnv = fs.readFileSync(rootEnvPath, "utf8");
  for (const line of rootEnv.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }
    const equalsIndex = trimmed.indexOf("=");
    if (equalsIndex <= 0) {
      continue;
    }
    const key = trimmed.slice(0, equalsIndex).trim();
    const value = trimmed.slice(equalsIndex + 1).trim();
    if (
      (key === "VITE_BEEVER_API_KEY" || key === "VITE_BEEVER_ADMIN_TOKEN") &&
      !env[key]
    ) {
      env[key] = value;
    }
  }
}

const result = spawnSync(process.execPath, [viteBin, ...viteArgs, "--config", configPath], {
  cwd: webDir,
  stdio: "inherit",
  env,
});

if (result.error) {
  throw result.error;
}

process.exit(result.status ?? 1);