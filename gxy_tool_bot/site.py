"""Static HTML site generation for GitHub Pages."""

from __future__ import annotations

import logging
from pathlib import Path

from gxy_tool_bot.config import SiteConfig

logger = logging.getLogger(__name__)


def generate_site(
    config: SiteConfig,
    output_dir: Path,
    issue_token: str,
) -> None:
    """
    Generate a static HTML site with a tool request form.
    The form creates GitHub issues directly via the Issues API.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    index_html = _build_index_html(config)
    style_css = _build_style_css()
    submit_js = _build_submit_js(config, issue_token)

    (output_dir / "index.html").write_text(index_html)
    (output_dir / "style.css").write_text(style_css)
    (output_dir / "submit.js").write_text(submit_js)

    logger.info("Site generated in %s", output_dir)


def _build_index_html(config: SiteConfig) -> str:
    description_html = ""
    if config.description:
        description_html = f'<p class="description">{config.description}</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{config.title}</title>
    <link rel="stylesheet" href="style.css">
</head>
<body>
    <main>
        <h1>{config.title}</h1>
        {description_html}
        <form id="tool-request-form">
            <div class="field">
                <label for="tool-name">Tool name *</label>
                <input type="text" id="tool-name" name="tool-name" required placeholder="e.g. samtools sort">
            </div>
            <div class="field">
                <label for="description">Description *</label>
                <textarea id="description" name="description" required rows="4" placeholder="What should the tool do?"></textarea>
            </div>
            <div class="field">
                <label for="links">Links (one per line)</label>
                <textarea id="links" name="links" rows="4" placeholder="https://github.com/...&#10;https://doi.org/...&#10;https://bioconda.org/..."></textarea>
            </div>
            <div class="field">
                <label for="contact">Contact (GitHub handle preferred)</label>
                <input type="text" id="contact" name="contact" placeholder="@your-github-handle">
            </div>
            <!-- Honeypot field for spam prevention -->
            <div class="honeypot">
                <label for="website">Website (leave empty)</label>
                <input type="text" id="website" name="website" tabindex="-1" autocomplete="off">
            </div>
            <button type="submit">Submit Request</button>
        </form>
        <div id="result" class="result" style="display:none;"></div>
    </main>
    <script src="submit.js"></script>
</body>
</html>"""


def _build_style_css() -> str:
    return """* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f6f8fa; color: #1f2328; line-height: 1.6; }
main { max-width: 640px; margin: 2rem auto; padding: 2rem; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
h1 { margin-bottom: 1rem; font-size: 1.5rem; }
.description { color: #656d76; margin-bottom: 1.5rem; }
.field { margin-bottom: 1rem; }
label { display: block; margin-bottom: 0.25rem; font-weight: 500; font-size: 0.9rem; }
input, textarea { width: 100%; padding: 0.5rem; border: 1px solid #d0d7de; border-radius: 6px; font-size: 0.9rem; }
input:focus, textarea:focus { outline: none; border-color: #0969da; box-shadow: 0 0 0 3px rgba(9,105,218,0.2); }
button { padding: 0.6rem 1.5rem; background: #2da44e; color: #fff; border: none; border-radius: 6px; font-size: 0.9rem; cursor: pointer; }
button:hover { background: #218bff; }
button:disabled { opacity: 0.6; cursor: not-allowed; }
.honeypot { position: absolute; left: -9999px; }
.result { margin-top: 1rem; padding: 1rem; border-radius: 6px; }
.result.success { background: #dafbe1; color: #1a7f37; }
.result.error { background: #ffebe9; color: #cf222e; }"""


def _build_submit_js(config: SiteConfig, issue_token: str) -> str:
    return f"""// gxy-tool-bot form submission
// Creates a GitHub issue directly via the Issues API
const REPO = "{config.repo}";
const TOKEN = "{issue_token}";
const LABEL = "tool-request";

document.getElementById("tool-request-form").addEventListener("submit", async (e) => {{
    e.preventDefault();
    const resultDiv = document.getElementById("result");
    resultDiv.style.display = "none";
    resultDiv.className = "result";

    // Honeypot check
    if (document.getElementById("website").value) {{
        return; // bot filled the honeypot
    }}

    const toolName = document.getElementById("tool-name").value.trim();
    const description = document.getElementById("description").value.trim();
    const linksText = document.getElementById("links").value.trim();
    const contact = document.getElementById("contact").value.trim();

    // Build issue body
    let body = `Tool name: ${{toolName}}\\n\\n`;
    body += `Description: ${{description}}\\n\\n`;
    if (linksText) {{
        body += `Links:\\n`;
        for (const link of linksText.split("\\n")) {{
            const l = link.trim();
            if (l) body += `- ${{l}}\\n`;
        }}
        body += "\\n";
    }}
    if (contact) {{
        body += `Contact: ${{contact}}\\n`;
    }}

    const btn = e.target.querySelector("button");
    btn.disabled = true;
    btn.textContent = "Submitting...";

    try {{
        const resp = await fetch(`https://api.github.com/repos/${{REPO}}/issues`, {{
            method: "POST",
            headers: {{
                "Authorization": `Bearer ${{TOKEN}}`,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            }},
            body: JSON.stringify({{
                title: `Tool request: ${{toolName}}`,
                body: body,
                labels: [LABEL],
            }}),
        }});

        if (!resp.ok) {{
            const err = await resp.json();
            throw new Error(err.message || `HTTP ${{resp.status}}`);
        }}

        const data = await resp.json();
        resultDiv.classList.add("success");
        resultDiv.textContent = `Request submitted! Track it here: ${{data.html_url}}`;
        resultDiv.style.display = "block";
        e.target.reset();
    }} catch (err) {{
        resultDiv.classList.add("error");
        resultDiv.textContent = `Error: ${{err.message}}`;
        resultDiv.style.display = "block";
    }} finally {{
        btn.disabled = false;
        btn.textContent = "Submit Request";
    }}
}});"""
