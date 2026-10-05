"""Register the optional MCP Apps control panel without adding authority."""

import os
import re
from importlib.resources import files
from typing import Any

from mcp.types import ToolAnnotations

from hermes_gpt.policy import runtime_settings

UI_ENV = "HERMES_GPT_ENABLE_CHATGPT_UI"
UI_URI = "ui://hermes/console/v6.html"
UI_MIME = "text/html;profile=mcp-app"


def ui_enabled() -> bool:
    return os.environ.get(UI_ENV) == "1"


def component_html() -> str:
    assets = files("hermes_gpt.clients.chatgpt").joinpath("assets")
    html = assets.joinpath("console.html").read_text(encoding="utf-8")
    for marker, name in (("/* PANEL_STYLE */", "console.css"),
                         ("/* HOST_BRIDGE */", "bridge.js"),
                         ("/* LAYOUT_SCRIPT */", "layout.js"),
                         ("/* PANEL_SCRIPT */", "console.js"),
                         ("/* JOB_SCRIPT */", "jobs.js"),
                         ("/* SETTINGS_SCRIPT */", "settings.js")):
        html = html.replace(marker, assets.joinpath(name).read_text(encoding="utf-8"))
    return html


def register_console(server: Any, *, core: Any, controls: Any, controls_enabled: bool) -> None:
    if not ui_enabled():
        return

    @server.resource(UI_URI, name="Hermes control panel", mime_type=UI_MIME,
                     meta={"ui": {"prefersBorder": True,
                                  "csp": {"connectDomains": [], "resourceDomains": []}}})
    def hermes_console_resource() -> str:
        """Self-contained panel. All operations use the host's existing MCP connection."""
        return component_html()

    from hermes_gpt.clients.chatgpt.management import connection_settings

    def hermes_console(job_id: str | None = None) -> dict[str, Any]:
        """Open the Hermes panel to ask for work, continue conversations, inspect schedules, and follow results. Supply job_id to follow existing work automatically."""
        if job_id is not None and not re.fullmatch(r"[a-f0-9]{32}", job_id):
            return {"success": False, "code": "INVALID_JOB_ID"}
        profiles = controls.hermes_session_profiles() if (runtime_settings.getenv("HERMES_GPT_ENABLE_SESSION_CONTROL") == "1" if runtime_settings.admin_enabled() else controls_enabled) else {"success": True, "profiles": []}
        return {"success": True, "console_version": "1", "profiles": profiles, "follow_job_id": job_id,
                "capabilities": core.capabilities(),
                "connection_settings": connection_settings()
                                       if runtime_settings.admin_enabled() else None}

    # Opening the component only reads configuration. Buttons call existing
    # tools, so they retain profile checks, annotations, confirmation, and logs.
    server.add_tool(hermes_console,
                    meta={"securitySchemes": [{"type": "noauth"}],
                          "ui": {"resourceUri": UI_URI},
                          "openai/outputTemplate": UI_URI},
                    annotations=ToolAnnotations(title="Open Hermes", readOnlyHint=True,
                                                destructiveHint=False))
