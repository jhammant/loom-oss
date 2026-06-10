// Minimal Node web app for Loom. Binds the port Loom injects via $PORT.
const http = require("http");

const port = process.env.PORT || 3000;
const name = process.env.LOOM_APP || "hello-web";

const server = http.createServer((req, res) => {
  if (req.url === "/healthz") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("ok");
    return;
  }
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(`<!doctype html>
<html>
  <head><title>${name} · Loom</title></head>
  <body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; line-height: 1.5;">
    <h1>👋 ${name}</h1>
    <p>This is a Node app running on the Loom fleet host.</p>
    <ul>
      <li>Served by host: <code>${req.headers.host}</code></li>
      <li>Listening on port: <code>${port}</code></li>
      <li>Runtime: <code>node</code></li>
    </ul>
  </body>
</html>`);
});

server.listen(port, () => {
  console.log(`[${name}] listening on :${port}`);
});
