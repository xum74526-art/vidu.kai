import {build} from "esbuild";
import {readFile, writeFile} from "node:fs/promises";
const result = await build({entryPoints:["mcp-panel.js"], bundle:true, minify:true, format:"esm", write:false, target:"es2022", metafile:true});
const js = result.outputFiles[0].text.replaceAll("</script", "<\\/script");
const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0;font:14px system-ui;color:#244f43}p{padding:16px}iframe{display:block;width:100%;height:930px;max-height:90vh;min-height:500px;border:0}iframe[hidden]{display:none}</style></head><body><p id="status">正在连接官方操作面板…</p><iframe id="official-panel" title="Vidu 官方操作面板" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin" hidden></iframe><script type="module">${js}</script></body></html>`;
await writeFile("../src/vidu_purchase/static/mcp-panel.html", html);
const packages = [...new Set(Object.keys(result.metafile.inputs)
  .map(path => path.match(/^node_modules\/((?:@[^/]+\/)?[^/]+)/)?.[1]).filter(Boolean))].sort();
const notices = ["Third-party code bundled into the Yingban MCP Apps resource.\n"];
for (const name of packages) {
  const root = `node_modules/${name}`;
  const pkg = JSON.parse(await readFile(`${root}/package.json`, "utf8"));
  let license;
  for (const file of ["LICENSE", "LICENSE.md", "LICENSE.txt"]) {
    try { license = await readFile(`${root}/${file}`, "utf8"); break; } catch {}
  }
  if (!license) throw new Error(`Missing license for bundled dependency ${name}`);
  notices.push(`${name} ${pkg.version}\n${"=".repeat(60)}\n${license}`);
}
await writeFile("THIRD_PARTY_NOTICES.txt", notices.join("\n\n"));
console.log(`Built MCP Apps resource (${Buffer.byteLength(html)} bytes)`);
