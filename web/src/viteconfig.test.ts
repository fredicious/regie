import config from "../vite.config";

test("the dev proxy presents the API's own Host and Origin", () => {
  // The server only answers Host 127.0.0.1:<port> or localhost:<port>, and checks Origin on commands.
  const proxy = config.server?.proxy?.["/api"];
  if (proxy === undefined || typeof proxy === "string") throw new Error(`no proxy options: ${String(proxy)}`);
  expect(proxy.target).toBe("http://127.0.0.1:7317");
  expect(proxy.changeOrigin).toBe(true);
  expect(proxy.headers?.origin).toBe("http://127.0.0.1:7317");
});
