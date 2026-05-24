import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const workspaceRoot = path.resolve(__dirname, "../..");
const webPackageJson = path.resolve(workspaceRoot, "web", "package.json");
const requireFromWeb = createRequire(webPackageJson);

const VIRTUAL_ID = "virtual:beever-plugin-chatgpt-overlay";
const RESOLVED_VIRTUAL_ID = `\0${VIRTUAL_ID}`;
const overlayFile = path.resolve(__dirname, "runtime", "chatgpt-overlay.ts");
const HARDCODED_API_FALLBACK = 'import.meta.env.VITE_API_URL || "http://localhost:8000"';
const SAME_ORIGIN_API_BASE = 'import.meta.env.VITE_API_URL ?? ""';

function rewriteApiBaseFallback(code: string): string {
  return code.replaceAll(HARDCODED_API_FALLBACK, SAME_ORIGIN_API_BASE);
}

function chatgptOverlayPlugin(ts: typeof import("typescript")) {
  return {
    name: "beever-plugin-chatgpt-overlay",
    enforce: "pre" as const,
    resolveId(id: string) {
      if (id === VIRTUAL_ID) {
        return RESOLVED_VIRTUAL_ID;
      }
      return null;
    },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    configureServer(server: any) {
      server.watcher.on("change", (file: string) => {
        if (path.resolve(file) === overlayFile) {
          const mod = server.moduleGraph.getModuleById(RESOLVED_VIRTUAL_ID);
          if (mod) {
            server.moduleGraph.invalidateModule(mod);
          }
          server.ws.send({ type: "full-reload" });
        }
      });
    },
    async load(id: string) {
      if (id === RESOLVED_VIRTUAL_ID) {
        const source = rewriteApiBaseFallback(fs.readFileSync(overlayFile, "utf8"));
        const result = ts.transpileModule(source, {
          compilerOptions: {
            module: ts.ModuleKind.ESNext,
            target: ts.ScriptTarget.ES2022,
          },
            fileName: overlayFile,
        });
        return result.outputText;
      }
      return null;
    },
    transform(code: string, id: string) {
      const normalizedId = id.replaceAll("\\", "/");
      const isWebSource = normalizedId.includes("/web/src/");
      const rewrittenCode = isWebSource ? rewriteApiBaseFallback(code) : code;

      if (!normalizedId.endsWith("/web/src/main.tsx")) {
        if (rewrittenCode !== code) {
          return rewrittenCode;
        }
        return null;
      }
      if (rewrittenCode.includes(VIRTUAL_ID)) {
        return null;
      }
      return `import ${JSON.stringify(VIRTUAL_ID)};\n${rewrittenCode}`;
    },
  };
}

export default async () => {
  const viteModule = await import(pathToFileURL(requireFromWeb.resolve("vite")).href);
  const reactModule = await import(
    pathToFileURL(requireFromWeb.resolve("@vitejs/plugin-react")).href,
  );
  const tailwindModule = await import(
    pathToFileURL(requireFromWeb.resolve("@tailwindcss/vite")).href,
  );

  const { defineConfig, mergeConfig } = viteModule;
  const react = reactModule.default;
  const tailwindcss = tailwindModule.default;
  const ts = requireFromWeb("typescript") as typeof import("typescript");

  return mergeConfig(
    defineConfig({
      envDir: path.resolve(workspaceRoot, "web"),
      resolve: {
        alias: {
          "@": path.resolve(workspaceRoot, "web", "src"),
        },
      },
      server: {
        fs: {
          allow: [workspaceRoot],
        },
        proxy: {
          "/api": {
            target: "http://localhost:8000",
            changeOrigin: true,
          },
        },
      },
      plugins: [react(), tailwindcss(), chatgptOverlayPlugin(ts)],
    }),
    defineConfig({}),
  );
};