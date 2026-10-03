export const PAGE_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>wristcall: pair your watch</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; }
  input, button { font-size: 1rem; padding: .5rem; }
  input { width: 100%; box-sizing: border-box; margin: .5rem 0; }
  #code { font-size: 2.5rem; letter-spacing: .2rem; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<h1>wristcall</h1>
<p>Enter the URL of your wristcall server. You get an 8 digit code, valid for 10 minutes, to type on the watch.</p>
<p>The server must be set to <code>pairing_approval: manual</code>. After typing the code on the watch, approve the request with <code>wristcall devices approve &lt;id&gt;</code>.</p>
<form id="f">
  <input id="url" type="url" placeholder="https://wristcall.yourdomain.com" required>
  <button>Generate code</button>
</form>
<p id="code"></p>
<p id="msg"></p>
<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = document.getElementById("msg");
  const out = document.getElementById("code");
  out.textContent = ""; msg.textContent = "";
  const r = await fetch("/v1/codes", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ url: document.getElementById("url").value }) });
  const body = await r.json();
  if (r.status !== 201) { msg.textContent = "Could not generate the code (" + (body.error || r.status) + ")."; return; }
  out.textContent = body.code.slice(0, 4) + " " + body.code.slice(4);
  msg.textContent = "Valid for 10 minutes, single use.";
});
</script>
</body>
</html>`;
