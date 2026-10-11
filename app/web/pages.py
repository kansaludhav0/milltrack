"""Plain server-rendered HTML pages, not JSON API endpoints — a public download page
and an Admin-only upload page for distributing the MillTrack APK. Deliberately outside
api_router/api_v1_prefix (these are pages a browser navigates to directly, not resources
an app client calls), and same-origin with the API they call, so no CORS setup is needed.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["web"])

_SHARED_STYLE = """
<style>
  :root {
    --bg: #eef0ec; --surface: #ffffff; --fg: #1b2420; --muted: #5c6460;
    --accent: #b5792e; --accent-ink: #fff8ee; --border: #d7dad4; --danger: #b3432f;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #11140f; --surface: #1a1f18; --fg: #e9ece6; --muted: #9aa39a;
      --accent: #d99a47; --accent-ink: #1b1306; --border: #2a2f26; --danger: #e08870; }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font-family: "IBM Plex Sans", system-ui, sans-serif;
    min-height: 100vh; display: flex; justify-content: center;
    padding: 48px 20px;
  }
  .wrap { width: 100%; max-width: 440px; display: flex; flex-direction: column; gap: 22px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
    padding: 36px 30px; display: flex; flex-direction: column; align-items: center;
    text-align: center; gap: 14px;
  }
  .eyebrow {
    font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 11px;
    letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent); font-weight: 600;
  }
  h1 { margin: 0; font-size: 34px; font-weight: 700; letter-spacing: -0.01em; }
  p.tagline { margin: 0; color: var(--muted); font-size: 15px; line-height: 1.5; max-width: 30ch; }
  .btn {
    margin-top: 10px; display: inline-flex; align-items: center; justify-content: center;
    gap: 10px; background: var(--accent); color: var(--accent-ink); font-weight: 600;
    font-size: 16px; padding: 15px 28px; border-radius: 10px; text-decoration: none;
    width: 100%; border: none; cursor: pointer; font-family: inherit;
  }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .hint { margin: 0; color: var(--muted); font-size: 12.5px; line-height: 1.5; max-width: 32ch; }
  .meta {
    font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px;
    color: var(--muted); line-height: 1.7;
  }
  .notes { font-size: 13.5px; color: var(--fg); background: var(--bg); border-radius: 8px;
    padding: 10px 14px; text-align: left; width: 100%; }
  .error { color: var(--danger); font-size: 13.5px; }
  form { width: 100%; display: flex; flex-direction: column; gap: 14px; text-align: left; }
  label { font-size: 12.5px; font-weight: 600; color: var(--muted); display: block; margin-bottom: 5px; }
  input, textarea {
    width: 100%; padding: 11px 12px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg); color: var(--fg); font-family: inherit; font-size: 14px;
  }
  textarea { resize: vertical; min-height: 60px; }
  .plate {
    font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 11px; letter-spacing: 0.08em;
    color: var(--muted); border-top: 1px solid var(--border); padding-top: 14px; width: 100%;
  }
</style>
"""

def _head(title: str) -> str:
    # Plain f-string substitution, not str.format() — _SHARED_STYLE's literal CSS
    # braces would otherwise be re-parsed as format fields and blow up.
    return f"""<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap">
<title>{title}</title>
{_SHARED_STYLE}</head>"""


DOWNLOAD_PAGE = f"""<!doctype html>
<html lang="en">
{_head("Get MillTrack")}
<body>
  <div class="wrap">
    <div class="card">
      <span class="eyebrow">Field App &middot; Android</span>
      <h1>MillTrack</h1>
      <p class="tagline">For gate, godown, supervisor and packaging teams on the floor.</p>

      <div id="state">Checking for the latest build&hellip;</div>

      <div class="plate">MILLTRACK&nbsp;&middot;&nbsp;SPINNING&nbsp;MILL&nbsp;TRACEABILITY&nbsp;SYSTEM</div>
    </div>
  </div>

  <script>
    const stateEl = document.getElementById('state');

    function renderAvailable(release) {{
      const notes = release.release_notes
        ? `<div class="notes">${{release.release_notes}}</div>` : '';
      stateEl.innerHTML = `
        <a class="btn" href="/api/v1/app/release/latest/download">Download for Android</a>
        <p class="hint">Version ${{release.version_label}} &middot; uploaded ${{new Date(release.uploaded_at).toLocaleDateString()}}</p>
        ${{notes}}
      `;
    }}

    function renderEmpty() {{
      stateEl.innerHTML = `
        <button class="btn" disabled>No build available yet</button>
        <p class="hint">Check back soon, or contact your Admin.</p>
      `;
    }}

    function renderError() {{
      stateEl.innerHTML = `<p class="error">Couldn't reach the server. Try reloading this page.</p>`;
    }}

    fetch('/api/v1/app/release/latest')
      .then(res => {{
        if (res.status === 404) {{ renderEmpty(); return null; }}
        if (!res.ok) throw new Error('bad response');
        return res.json();
      }})
      .then(release => {{ if (release) renderAvailable(release); }})
      .catch(renderError);
  </script>
</body>
</html>
"""


UPLOAD_PAGE = f"""<!doctype html>
<html lang="en">
{_head("Upload MillTrack Build")}
<body>
  <div class="wrap">
    <div class="card">
      <span class="eyebrow">Admin Only</span>
      <h1>Upload Build</h1>
      <p class="tagline">Publish a new APK. It replaces whatever was there before.</p>

      <div id="login-step">
        <form id="login-form">
          <div>
            <label for="email">Admin email</label>
            <input type="email" id="email" required autocomplete="username">
          </div>
          <div>
            <label for="password">Password</label>
            <input type="password" id="password" required autocomplete="current-password">
          </div>
          <button class="btn" type="submit">Sign in</button>
          <p class="error" id="login-error"></p>
        </form>
      </div>

      <div id="upload-step" style="display:none; width:100%;">
        <form id="upload-form">
          <div>
            <label for="version">Version label</label>
            <input type="text" id="version" placeholder="1.4.2" required>
          </div>
          <div>
            <label for="notes">Release notes (optional)</label>
            <textarea id="notes" placeholder="Fixed the mixing-complete button"></textarea>
          </div>
          <div>
            <label for="file">APK file</label>
            <input type="file" id="file" accept=".apk" required>
          </div>
          <button class="btn" type="submit" id="upload-btn">Upload &amp; publish</button>
          <p class="error" id="upload-error"></p>
          <p class="hint" id="upload-success"></p>
        </form>
      </div>
    </div>
  </div>

  <script>
    let token = null;

    document.getElementById('login-form').addEventListener('submit', async (e) => {{
      e.preventDefault();
      const errEl = document.getElementById('login-error');
      errEl.textContent = '';
      const email = document.getElementById('email').value;
      const password = document.getElementById('password').value;
      try {{
        const res = await fetch('/api/v1/auth/login', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ email, secret: password }}),
        }});
        if (!res.ok) {{
          const body = await res.json().catch(() => ({{}}));
          throw new Error(body.detail || 'Login failed');
        }}
        const data = await res.json();
        if (data.role !== 'GM_MD_ADMIN') {{
          throw new Error('This account is not an Admin account.');
        }}
        token = data.access_token;
        document.getElementById('login-step').style.display = 'none';
        document.getElementById('upload-step').style.display = 'block';
      }} catch (err) {{
        errEl.textContent = err.message;
      }}
    }});

    document.getElementById('upload-form').addEventListener('submit', async (e) => {{
      e.preventDefault();
      const errEl = document.getElementById('upload-error');
      const successEl = document.getElementById('upload-success');
      const btn = document.getElementById('upload-btn');
      errEl.textContent = '';
      successEl.textContent = '';

      const fileInput = document.getElementById('file');
      if (!fileInput.files.length) {{ errEl.textContent = 'Choose a .apk file.'; return; }}

      const formData = new FormData();
      formData.append('version_label', document.getElementById('version').value);
      formData.append('release_notes', document.getElementById('notes').value);
      formData.append('file', fileInput.files[0]);

      btn.disabled = true;
      btn.textContent = 'Uploading…';
      try {{
        const res = await fetch('/api/v1/app/release', {{
          method: 'POST',
          headers: {{ 'Authorization': `Bearer ${{token}}` }},
          body: formData,
        }});
        if (!res.ok) {{
          const body = await res.json().catch(() => ({{}}));
          throw new Error(body.detail || 'Upload failed');
        }}
        const data = await res.json();
        successEl.textContent = `Published version ${{data.version_label}}. Staff can now download it.`;
        document.getElementById('upload-form').reset();
      }} catch (err) {{
        errEl.textContent = err.message;
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Upload & publish';
      }}
    }});
  </script>
</body>
</html>
"""


@router.get("/download", response_class=HTMLResponse, summary="Public APK download page")
async def download_page() -> str:
    return DOWNLOAD_PAGE


@router.get("/upload", response_class=HTMLResponse, summary="Admin-only APK upload page")
async def upload_page() -> str:
    return UPLOAD_PAGE
