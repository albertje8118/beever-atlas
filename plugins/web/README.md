# Plugin Web Overlay

This fork keeps ChatGPT UI changes out of `web/src` by loading a plugin-owned overlay module at Vite startup.

Use the plugin-owned wrapper when starting the frontend:

```powershell
node plugins/web/run-vite.mjs dev
```

For a production build with the overlay included:

```powershell
node plugins/web/run-vite.mjs build
```

Run the backend in plugin mode as usual:

```powershell
uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000
```

The overlay adds a ChatGPT connection entry point to the Settings and welcome screens and drives the plugin-owned `/api/plugins/chatgpt/*` endpoints without modifying the upstream React source tree.

If you want a separate typecheck before building, run:

```powershell
cd web
npm run typecheck
```