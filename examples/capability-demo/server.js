// Loom capability-demo: the reference app for the v2 contract. It serves a
// health endpoint, a declared http capability (/search), and an OpenAPI spec.
const http = require("http");

const port = process.env.PORT || 3000;
const name = process.env.LOOM_APP || "capability-demo";

const ITEMS = ["loom", "fleet", "harvester", "library", "gateway", "contract"];

const OPENAPI = {
  openapi: "3.1.0",
  info: { title: name, version: "1.0.0", description: "Demo search capability." },
  paths: {
    "/search": {
      get: {
        operationId: "search",
        summary: "Search the demo corpus",
        parameters: [{ name: "q", in: "query", required: true, schema: { type: "string" } }],
        responses: { "200": { description: "matching items" } },
      },
    },
  },
};

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const json = (code, body) => {
    res.writeHead(code, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (url.pathname === "/health") return json(200, { status: "ok" });
  if (url.pathname === "/openapi.json") return json(200, OPENAPI);
  if (url.pathname === "/search") {
    const q = (url.searchParams.get("q") || "").toLowerCase();
    return json(200, { query: q, results: ITEMS.filter((i) => i.includes(q)) });
  }
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(`<!doctype html><html><head><title>${name} · Loom</title></head>
  <body style="font-family:system-ui;max-width:40rem;margin:4rem auto">
  <h1>🧩 ${name}</h1><p>A Loom v2-contract app. Try <code>/search?q=lo</code>,
  <code>/openapi.json</code>, <code>/health</code>.</p></body></html>`);
});

server.listen(port, () => console.log(`[${name}] listening on :${port}`));
