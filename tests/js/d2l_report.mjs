// Prints the Brightspace diagnostic (extension/d2l.js) run against the mock Brightspace, as JSON on
// one line, so the Python tests can post exactly what the extension would send.
import { startMockD2L, NOW } from "../../extension/tests/mock-d2l.mjs";
import { diagnoseBrightspace } from "../../extension/d2l.js";

const mock = await startMockD2L();
try {
  const get = async (url, { signal } = {}) => {
    const r = await fetch(url, { headers: { Cookie: "d2lSessionVal=valid" }, signal });
    return { status: r.status, text: await r.text(), headers: { "retry-after": r.headers.get("retry-after") } };
  };
  const report = await diagnoseBrightspace({ baseUrl: mock.url, get, now: NOW, extensionVersion: "1.5.0", transport: "worker" });
  console.log(JSON.stringify(report));
} finally {
  await mock.close();
}
